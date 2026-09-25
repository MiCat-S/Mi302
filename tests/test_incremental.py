"""增量同步、刪除過期項目、115 帳號狀態。115 以一個可變的假目錄樹模擬。"""

import json
import time
from pathlib import Path

import httpx

from embyserver.config import P115StrmConfig, StrmTask
from embyserver.db import Database
from embyserver.p115 import P115Service
from embyserver.strm_sync import INCREMENTAL, StrmSync

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
        self.calls.append((request.url.path, dict(p)))
        if request.url.path == "/files/getid":
            for cid in self.dirs:
                if cid and "/" + "/".join(a["name"] for a in self.ancestors(cid)[1:]) == p["path"]:
                    return httpx.Response(200, json={"state": True, "id": str(cid)})
            return httpx.Response(200, json={"state": True, "id": "0"})
        if request.url.path == "/files":
            cid = int(p["cid"])
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
    # 改名會更新修改時間
    fake.files[0].update(n="Old Movie (2001) 4K.mkv", te=T0 + 9000)
    r = sync.run(INCREMENTAL)
    assert (tmp_path / "media" / "電影" / "Old Movie (2001) 4K.strm").exists()
    assert len(r.new_files) == 1


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
