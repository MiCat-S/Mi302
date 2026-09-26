"""增量同步（生活事件＋修改時間）、刪除過期項目、115 帳號狀態。115 以一個可變的假目錄樹模擬。"""

import json
import time
from pathlib import Path

import httpx

from embyserver.config import P115StrmConfig, StrmTask
from embyserver.db import Database
from embyserver.p115 import P115Service
from embyserver.strm_sync import FULL, INCREMENTAL, StrmSync, _sidecars

T0 = 1_700_000_000


class Fake115:
    """cid → (名稱, 上層 cid)；檔案 {fid, cid, n, pc, s, te}。"""

    def __init__(self):
        self.dirs = {0: ("根目录", None), 100: ("影視", 0), 101: ("電影", 100), 102: ("劇集", 100), 103: ("Dark", 102)}
        self.files = [
            {"fid": 1, "cid": 101, "n": "Old Movie (2001).mkv", "pc": "a" * 17, "s": 900_000_000, "te": T0},
            {"fid": 2, "cid": 103, "n": "Dark.S01E01.mkv", "pc": "b" * 17, "s": 900_000_000, "te": T0 + 10},
        ]
        self.calls = []
        self.events = []  # 生活事件，由舊到新
        self.life_enabled = False
        self.export_ok = True  # 支援導出目錄樹
        self.exports = {}  # export_id → [cid, 還要輪詢幾次]
        self.deleted = []

    def tree(self, cid):
        """115 導出目錄樹的格式：根目录、導出的資料夾，再往下每層多一個「| 」。"""
        lines = ["|——根目录", "| |-" + self.dirs[cid][0]]

        def rec(c, depth):
            for d, (name, parent) in self.dirs.items():
                if parent == c:
                    lines.append("| " * depth + "|-" + name)
                    rec(d, depth + 1)
            lines.extend("| " * depth + "|-" + f["n"] for f in self.files if f["cid"] == c)

        rec(cid, 2)
        return ("\n".join(lines) + "\n").encode("utf-16")

    # ---- 在假 115 上操作，同時記一筆生活事件（不改修改時間，確定是靠事件抓到的） ----
    def event(self, type_, fid, is_dir=False):
        if is_dir:
            name, parent = self.dirs.get(fid, ("", 0))
            pc, size = "", 0
        else:
            f = next((f for f in self.files if f["fid"] == fid), None) or {"n": "", "cid": 0, "pc": "", "s": 0}
            name, parent, pc, size = f["n"], f["cid"], f["pc"], f["s"]
        self.events.append({
            "id": str(1000 + len(self.events)), "type": type_, "file_id": str(fid), "parent_id": str(parent),
            "file_name": name, "file_category": "0" if is_dir else "1", "pick_code": pc, "file_size": size,
            "update_time": T0 + 100 + len(self.events),
        })

    def file(self, fid):
        return next(f for f in self.files if f["fid"] == fid)

    def ancestors(self, cid):
        chain = []
        while cid is not None:
            name, parent = self.dirs[cid]
            chain.append({"cid": cid, "name": name})
            cid = parent
        return list(reversed(chain))

    def under(self, cid, target):
        while target is not None:
            if target == cid:
                return True
            target = self.dirs[target][1]
        return False

    def handler(self, request: httpx.Request) -> httpx.Response:
        p = request.url.params
        if request.url.host == "life.115.com":
            self.life_enabled = True
            return httpx.Response(200, json={"state": True})
        if request.url.path == "/behavior/detail":
            evs = list(reversed(self.events))
            offset, limit = int(p.get("offset", 0)), int(p.get("limit", 1000))
            return httpx.Response(200, json={"state": True, "data": {
                "count": len(evs), "list": evs[offset: offset + limit]}})
        self.calls.append((request.url.path, dict(p)))
        if request.url.host == "cdn.115.test" and request.url.path.startswith("/tree"):
            return httpx.Response(200, content=self.tree(int(request.url.path[5:])))
        if request.url.path == "/files/export_dir":
            if not self.export_ok:
                return httpx.Response(200, json={"state": False, "error": "已有导出任务在进行"})
            if request.method == "POST":
                form = dict(httpx.QueryParams(request.content.decode()))
                eid = str(500 + len(self.exports))
                self.exports[eid] = [int(form["file_ids"]), 1]
                return httpx.Response(200, json={"state": True, "data": {"export_id": eid}})
            job = self.exports[p["export_id"]]
            if job[1] > 0:  # 還在產生
                job[1] -= 1
                return httpx.Response(200, json={"state": True, "data": []})
            return httpx.Response(200, json={"state": True, "data": {
                "export_id": p["export_id"], "file_id": "9" + p["export_id"], "file_name": "目录树.txt",
                "pick_code": f"tree{job[0]}"}})
        if request.url.path == "/rb/delete":
            self.deleted.append(dict(httpx.QueryParams(request.content.decode()))["fid[0]"])
            return httpx.Response(200, json={"state": True})
        if request.url.path == "/files/getid":
            for cid in self.dirs:
                if cid and "/" + "/".join(a["name"] for a in self.ancestors(cid)[1:]) == p["path"]:
                    return httpx.Response(200, json={"state": True, "id": str(cid)})
            return httpx.Response(200, json={"state": True, "id": "0"})
        if request.url.path == "/files":
            cid = int(p["cid"])
            if cid not in self.dirs:  # 不存在的目錄，115 會回根目錄
                return httpx.Response(200, json={"state": True, "count": 0, "data": [], "path": self.ancestors(0)})
            offset, limit = int(p.get("offset", 0)), int(p.get("limit", 1150))
            if p.get("cur") == "0":
                items = sorted(
                    (f for f in self.files if self.under(cid, f["cid"])), key=lambda f: f["te"], reverse=True
                )
            else:
                items = [{"cid": c, "n": n} for c, (n, parent) in self.dirs.items() if parent == cid]
                items += [f for f in self.files if f["cid"] == cid]
            page = items[offset: offset + limit]
            return httpx.Response(
                200, json={"state": True, "count": len(items), "data": page, "path": self.ancestors(cid)}
            )
        return httpx.Response(404)


def make(tmp_path: Path, fake: Fake115, **kw) -> StrmSync:
    svc = P115Service(Database(":memory:"), initial_cookies="UID=1", transport=httpx.MockTransport(fake.handler))
    svc.download_url = lambda pc, ua="": f"https://cdn.115.test/{pc}"
    svc.export_poll = 0
    cfg = P115StrmConfig(tasks=[StrmTask(remote="/影視", local=str(tmp_path / "media"))], request_delay=0, **kw)
    sync = StrmSync(svc, cfg)
    sync._http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<nfo>")))
    return sync


def test_incremental_only_touches_new_files(tmp_path: Path):
    fake = Fake115()
    sync = make(tmp_path, fake)

    # 第一次增量同步沒有基準點，會先全量
    r = sync.run(INCREMENTAL)
    assert r.fell_back_to_full == ["/影視"] and r.strm_created == 2 and len(r.new_files) == 2
    assert sync.task_states()[0]["since"] == T0 + 10

    # 115 上新增一集、一部新片（在新資料夾裡）
    fake.dirs[104] = ("New (2024)", 101)
    fake.files.append({"fid": 3, "cid": 103, "n": "Dark.S01E02.mkv", "pc": "c" * 17, "s": 900_000_000, "te": T0 + 5000})
    fake.files.append({"fid": 4, "cid": 104, "n": "New (2024).mp4", "pc": "d" * 17, "s": 900_000_000, "te": T0 + 6000})
    fake.files.append({"fid": 5, "cid": 104, "n": "movie.nfo", "pc": "e" * 17, "s": 5, "te": T0 + 6001})
    fake.calls.clear()

    r = sync.run(INCREMENTAL)
    media = tmp_path / "media"
    assert (media / "劇集" / "Dark" / "Dark.S01E02.strm").read_text().endswith(f"/d/{'c' * 17}.mkv")
    assert (media / "電影" / "New (2024)" / "New (2024).strm").exists()
    assert (media / "電影" / "New (2024)" / "movie.nfo").read_bytes() == b"<nfo>"
    assert sorted(Path(f).name for f in r.new_files) == ["Dark.S01E02.strm", "New (2024).strm"]
    assert not r.errors and not r.fell_back_to_full
    # 增量不逐層列目錄：只有一次遞迴列檔，加上查新資料夾的路徑
    listings = [c for c in fake.calls if c[0] == "/files" and c[1].get("cur") != "0" and c[1].get("limit") != "1"]
    assert listings == []
    assert sync.task_states()[0]["since"] == T0 + 6001

    # 沒有新東西時，只打一次遞迴列檔（加上查任務目錄 id）；
    # 往回重疊的那段時間裡的檔案會再看一次，但內容沒變不會重寫
    fake.calls.clear()
    r = sync.run(INCREMENTAL)
    assert r.strm_created == 0 and not r.new_files and r.strm_unchanged == 1
    assert [c[0] for c in fake.calls if c[1].get("limit") != "1"] == ["/files/getid", "/files"]


def test_incremental_picks_up_renamed_file(tmp_path: Path):
    fake = Fake115()
    sync = make(tmp_path, fake)
    sync.run()
    movies = tmp_path / "media" / "電影"
    (movies / "Old Movie (2001).nfo").write_text("<movie/>")  # MoviePilot 刮削的
    # 沒有生活事件也抓得到：改名會更新修改時間
    fake.files[0].update(n="Old Movie (2001) 4K.mkv", te=T0 + 9000)
    r = sync.run(INCREMENTAL)
    assert (movies / "Old Movie (2001) 4K.strm").exists()
    assert not (movies / "Old Movie (2001).strm").exists()
    assert (movies / "Old Movie (2001) 4K.nfo").read_text() == "<movie/>"  # 刮削資料跟著改名
    assert r.moved == 1 and not r.new_files  # 不是新片，不會再送去刮削


def test_delete_stale_keeps_moviepilot_metadata(tmp_path: Path):
    fake = Fake115()
    sync = make(tmp_path, fake, delete_stale=True)
    sync.run()
    media = tmp_path / "media"
    # MoviePilot 刮削寫進來的檔案
    (media / "劇集" / "Dark" / "tvshow.nfo").write_text("<tvshow/>")
    (media / "劇集" / "Dark" / "Dark.S01E01.nfo").write_text("<ep/>")
    (media / "電影" / "Old Movie (2001).nfo").write_text("<movie/>")
    (media / "電影" / "Old Movie (2001)-poster.jpg").write_bytes(b"jpg")
    (media / "電影" / "notes.txt").write_text("mine")

    # 115 上刪掉電影，劇集不動
    fake.files = [f for f in fake.files if f["fid"] != 1]
    r = sync.run()
    assert not (media / "電影" / "Old Movie (2001).strm").exists()
    assert not (media / "電影" / "Old Movie (2001).nfo").exists()
    assert not (media / "電影" / "Old Movie (2001)-poster.jpg").exists()
    assert (media / "電影" / "notes.txt").exists()  # 不是中繼資料，不動
    assert (media / "劇集" / "Dark" / "tvshow.nfo").exists()
    assert (media / "劇集" / "Dark" / "Dark.S01E01.nfo").exists()
    assert r.removed == 3

    # 整部劇被刪：資料夾裡的中繼資料一起清掉，空資料夾移除
    fake.files = []
    sync.run()
    assert not (media / "劇集" / "Dark").exists()


def test_account_info():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "my.115.com":
            return httpx.Response(200, json={"state": True, "data": {
                "uid": 123, "uname": "cat",
                "face": {"face_m": "https://face/m.jpg"},
                "vip": {"is_vip": 1, "is_forever": 0, "expire_str": "2027-01-01", "level_name": "年費VIP"},
            }})
        if request.url.path == "/files/index_info":
            return httpx.Response(200, json={"state": True, "data": {"space_info": {
                "all_total": {"size": 1000, "size_format": "1000B"},
                "all_use": {"size": 250}, "all_remain": {"size": 750},
            }}})
        if "login_devices" in request.url.path:
            return httpx.Response(200, json={"state": True, "data": {"list": [
                {"name": "支付寶小程式", "ssoent": "R1", "ip": "1.2.3.4", "utime": T0, "is_current": 1},
            ]}})
        return httpx.Response(404)

    svc = P115Service(Database(":memory:"), transport=httpx.MockTransport(handler))
    svc.set_cookies("UID=123_A1; CID=x; SEID=y", source="qrcode")
    info = svc.account_info()
    assert info["account"]["user_name"] == "cat" and info["account"]["user_id"] == "123"
    assert info["account"]["vip"] == {"is_vip": True, "is_forever": False, "expire": "2027-01-01", "level": "年費VIP"}
    assert info["account"]["space"] == {"total": 1000, "used": 250, "remain": 750}
    assert info["login"]["method"] == "qrcode" and info["login"]["app"] == "alipaymini"
    assert info["cookie"]["devices"][0]["current"] is True
    assert svc.account_info() is info  # 一分鐘內用快取


def test_account_info_expired_cookie():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": False, "message": "请重新登录"})

    svc = P115Service(Database(":memory:"), transport=httpx.MockTransport(handler))
    svc.set_cookies("UID=1", source="cookie")
    info = svc.account_info()
    assert info["cookie"]["valid"] is False and "重新登录" in info["cookie"]["error"]
    assert info["account"] is None


def life_sync(tmp_path: Path, **kw):
    fake = Fake115()
    fake.event(2, 2)  # 之前的舊事件
    sync = make(tmp_path, fake, delete_stale=True, **kw)
    r = sync.run(INCREMENTAL)  # 第一次：全量，並記下目前最新的事件
    assert r.fell_back_to_full == ["/影視"] and fake.life_enabled
    assert sync.task_states()[0]["life_id"] == 1000
    return fake, sync, tmp_path / "media"


def test_life_events_upload_move_rename_delete(tmp_path: Path):
    fake, sync, media = life_sync(tmp_path)
    (media / "劇集" / "Dark" / "tvshow.nfo").write_text("<tvshow/>")
    (media / "劇集" / "Dark" / "Dark.S01E01.nfo").write_text("<ep/>")
    (media / "電影" / "Old Movie (2001).nfo").write_text("<movie/>")
    (media / "電影" / "Old Movie (2001)-poster.jpg").write_bytes(b"jpg")

    # 上傳新片（修改時間很舊，只有事件抓得到）
    fake.files.append({"fid": 7, "cid": 101, "n": "Up (2009).mkv", "pc": "u" * 17, "s": 900_000_000, "te": T0 - 99999})
    fake.event(2, 7)
    # 電影移到新資料夾
    fake.dirs[105] = ("Old Movie (2001)", 101)
    fake.event(17, 105, is_dir=True)
    fake.file(1)["cid"] = 105
    fake.event(6, 1)
    # 劇集資料夾改名
    fake.dirs[103] = ("Dark (2017)", 102)
    fake.event(20, 103, is_dir=True)
    # 放到任務目錄外面的上傳不理
    fake.dirs[200] = ("待整理", 0)
    fake.files.append({"fid": 8, "cid": 200, "n": "x.mkv", "pc": "x" * 17, "s": 900_000_000, "te": T0})
    fake.event(2, 8)
    fake.calls.clear()

    r = sync.run(INCREMENTAL)
    assert not r.errors and not r.fell_back_to_full and r.events == 5
    assert [Path(f).name for f in r.new_files] == ["Up (2009).strm"]
    moved = media / "電影" / "Old Movie (2001)"
    assert (moved / "Old Movie (2001).strm").exists() and not (media / "電影" / "Old Movie (2001).strm").exists()
    assert (moved / "Old Movie (2001).nfo").read_text() == "<movie/>"
    assert (moved / "Old Movie (2001)-poster.jpg").exists()
    show = media / "劇集" / "Dark (2017)"
    assert (show / "tvshow.nfo").exists() and (show / "Dark.S01E01.strm").exists()
    assert not (media / "劇集" / "Dark").exists()
    assert r.moved == 2 and sync.task_states()[0]["life_id"] == 1005
    assert not (media / "待整理").exists()

    # 刪除：strm 連同刮削資料一起刪
    fake.files = [f for f in fake.files if f["fid"] != 1]
    fake.event(22, 1)
    fake.files = [f for f in fake.files if f["cid"] != 103]
    del fake.dirs[103]
    fake.event(22, 103, is_dir=True)
    r = sync.run(INCREMENTAL)
    assert not r.errors and r.removed >= 5
    assert not moved.exists() and not show.exists()
    assert (media / "電影" / "Up (2009).strm").exists()


def test_life_events_folder_moved_in_and_out(tmp_path: Path):
    fake, sync, media = life_sync(tmp_path)
    # 從任務目錄外面移進一整個資料夾（MoviePilot 整理完常這樣）
    fake.dirs[300] = ("Arrival (2016)", 0)
    fake.files.append({"fid": 9, "cid": 300, "n": "Arrival (2016).mkv", "pc": "r" * 17, "s": 900_000_000, "te": T0})
    fake.dirs[300] = ("Arrival (2016)", 101)
    fake.event(6, 300, is_dir=True)
    r = sync.run(INCREMENTAL)
    assert (media / "電影" / "Arrival (2016)" / "Arrival (2016).strm").exists()
    assert [Path(f).name for f in r.new_files] == ["Arrival (2016).strm"]

    # 再移出任務目錄：開了 delete_stale 就刪掉
    fake.dirs[300] = ("Arrival (2016)", 200)
    fake.dirs[200] = ("待整理", 0)
    fake.event(6, 300, is_dir=True)
    sync.run(INCREMENTAL)
    assert not (media / "電影" / "Arrival (2016)").exists()


def test_life_event_gap_falls_back_to_full(tmp_path: Path):
    fake, sync, media = life_sync(tmp_path)
    # 115 只保留最新的事件：上次讀到的位置已經不在清單裡
    fake.events = [dict(e, id=str(5000 + i)) for i, e in enumerate(fake.events)]
    fake.event(2, 2)
    fake.events[-1]["id"] = "6000"
    r = sync.run(INCREMENTAL)
    assert r.fell_back_to_full == ["/影視"] and "超過 115 保留的範圍" in r.notes[0]
    assert sync.task_states()[0]["life_id"] == 6000


def test_full_schedule_uses_last_full_time(tmp_path: Path):
    fake = Fake115()
    sync = make(tmp_path, fake, full_interval=168)
    assert not sync._full_due(time.time())  # 還沒同步過，不搶著跑
    sync.run(FULL)
    now = time.time()
    assert not sync._full_due(now + 167 * 3600)
    assert sync._full_due(now + 169 * 3600)


def test_background_sync_reports_running_immediately(tmp_path: Path):
    fake = Fake115()
    sync = make(tmp_path, fake)
    before = time.time()
    assert sync.run_in_background(FULL)
    # 網頁按下同步後馬上查狀態，看到的就是這一次（不會是上一次的結果）
    started = sync.result.started
    assert started >= before and sync.result.mode == FULL
    assert not sync.run_in_background(INCREMENTAL) or sync.result.started != started
    deadline = time.time() + 10
    while sync._lock.locked() and time.time() < deadline:
        time.sleep(0.01)
    assert not sync.result.running and sync.result.finished >= started


def test_sidecars_do_not_steal_longer_names(tmp_path: Path):
    for name in ["Movie.strm", "Movie.nfo", "Movie-poster.jpg", "Movie-2.strm", "Movie-2.nfo", "Movie-2-poster.jpg", "notes.txt"]:
        (tmp_path / name).write_text("x")
    assert sorted(f.name for f in _sidecars(tmp_path, "Movie")) == ["Movie-poster.jpg", "Movie.nfo"]


def test_life_events_paging_and_filtering():
    fake = Fake115()
    for i in range(100):
        fake.event(8 if i % 10 == 0 else 2, 1)  # 8 = 瀏覽影片，不處理
    svc = P115Service(Database(":memory:"), initial_cookies="UID=1", transport=httpx.MockTransport(fake.handler))
    evs = svc.life_events(1049, int(time.time()) - 60)
    assert [e["id"] for e in evs] == [i for i in range(1051, 1100) if (i - 1000) % 10]
    assert evs[0]["name"] == "Old Movie (2001).mkv" and evs[0]["pickcode"] == "a" * 17 and not evs[0]["is_dir"]
    assert svc.latest_life_event()[0] == 1099


def test_life_event_keeps_long_ids_exact():
    from embyserver.p115 import _life_event

    ev = _life_event({"id": "3006728302924193796", "file_id": "3006728302924193797", "parent_id": "2593093001609739968"})
    # 115 的 id 有 19 位數，不能先轉成浮點數
    assert (ev["id"], ev["file_id"], ev["parent_id"]) == (3006728302924193796, 3006728302924193797, 2593093001609739968)
