"""測試共用的假 115（cookie 和開放平台）、假的媒體資訊，以及建測試環境的輔助函式。

各測試檔從這裡 import，不互相借用。只有一個測試檔用到的假物件留在那個檔案裡。
"""

import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import P115StrmConfig, StrmTask, config_from_dict
from embyserver.db import Database
from embyserver.p115 import P115Service
from embyserver.strm_sync import FULL, StrmSync


# ---------------- 115（cookie）----------------

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
            form = dict(httpx.QueryParams(request.content.decode()))
            ids = [v for k, v in form.items() if k.startswith("fid[")]
            self.deleted += ids
            self.files = [f for f in self.files if str(f["fid"]) not in ids]  # 送進回收站就不在清單上了
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


# ---------------- 115 開放平台 ----------------

OPEN_PC = "abcdefghijklmnopq"
OPEN_CDN = "https://cdnfhnfile.115cdn.net/x/a.mkv?t=4102444800"


class FakeOpen115:
    def __init__(self):
        self.calls = []
        self.token_version = 1
        self.expire_next = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        form = parse_qs(request.content.decode()) if request.content else {}
        if path == "/open/authDeviceCode":
            assert form["client_id"] == ["app1"] and form["code_challenge_method"] == ["sha256"]
            self.challenge = form["code_challenge"][0]
            return httpx.Response(200, json={"state": 1, "data": {"uid": "u1", "time": 1, "sign": "s"}})
        if path == "/get/status/":
            return httpx.Response(200, json={"state": 1, "data": {"status": 2}})
        if path == "/open/deviceCodeToToken":
            import base64, hashlib
            verifier = form["code_verifier"][0]
            expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            assert expect == self.challenge  # PKCE 驗證
            return httpx.Response(200, json={"state": 1, "data": {"access_token": "at1", "refresh_token": "rt1", "expires_in": 7200}})
        if path == "/open/refreshToken":
            self.token_version += 1
            return httpx.Response(200, json={"state": 1, "data": {
                "access_token": f"at{self.token_version}", "refresh_token": f"rt{self.token_version}", "expires_in": 7200}})
        auth = request.headers.get("authorization", "")
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(200, json={"state": 0, "code": 40140125, "message": "token expired"})
        if path == "/open/ufile/downurl":
            assert form["pick_code"] == [OPEN_PC] and auth.startswith("Bearer at")
            return httpx.Response(200, json={"state": True, "data": {"123": {"url": {"url": OPEN_CDN}}}})
        if path == "/open/folder/get_info":
            return httpx.Response(200, json={"state": True, "data": {"file_id": "100"}})
        if path == "/open/ufile/files":
            return httpx.Response(200, json={"state": True, "count": 2, "path": [{"cid": 0}, {"cid": 100}], "data": [
                {"fid": "101", "fn": "電影", "fc": "0"},
                {"fid": "5", "fn": "a.mkv", "fc": "1", "pc": OPEN_PC, "fs": 1000},
            ]})
        return httpx.Response(404)


# ---------------- 重複檔案（找重複、停止）----------------

SHA_MOVIE = "A" * 40
SHA_EP = "B" * 40


def build_dupes(tmp_path: Path):
    fake = Fake115()
    fake.dirs.update({104: ("Old Movie Copy", 101), 200: ("待整理", 0)})
    fake.file(1)["sha"] = SHA_MOVIE
    fake.file(2)["sha"] = SHA_EP
    fake.files += [
        # 同一部電影轉存了兩次，都在同步目錄裡：兩份都有 strm，早上傳的那份建議保留
        {"fid": 3, "cid": 104, "n": "Old Movie (2001).mkv", "pc": "c" * 17, "s": 900_000_000, "te": T0 + 50, "sha": SHA_MOVIE},
        # 同步目錄外面的一份（待整理）：沒有 strm
        {"fid": 8, "cid": 200, "n": "Dark.S01E01.mkv", "pc": "d" * 17, "s": 900_000_000, "te": T0 + 60, "sha": SHA_EP},
        # 不是影片、或大小不同的，不算重複
        {"fid": 9, "cid": 200, "n": "poster.jpg", "pc": "e" * 17, "s": 10, "te": T0, "sha": "C" * 40},
        {"fid": 10, "cid": 200, "n": "poster copy.jpg", "pc": "f" * 17, "s": 10, "te": T0, "sha": "C" * 40},
        {"fid": 11, "cid": 200, "n": "other.mkv", "pc": "g" * 17, "s": 123, "te": T0, "sha": SHA_MOVIE},
    ]
    media = tmp_path / "media"
    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")},
        "users": [{"name": "admin", "password": "pw", "admin": True}],
        "libraries": [
            {"name": "電影", "type": "movies", "paths": [str(media / "電影")]},
            {"name": "劇集", "type": "tvshows", "paths": [str(media / "劇集")]},
        ],
        "p115": {"cookies": "UID=1", "strm": {"tasks": [{"remote": "/影視", "local": str(media)}], "request_delay": 0,
                                               "scan_after_sync": False}},
    }), scan_on_start=False)
    # 換成接假 115 的客戶端（一開始就指定 transport；事後換 _transport 會被環境變數裡的代理繞過去）
    svc = P115Service(app.state.db, initial_cookies="UID=1", transport=httpx.MockTransport(fake.handler))
    for holder in (app.state, app.state.strm_sync, app.state.dupes, app.state.prober, app.state.redirector):
        holder.p115 = svc
    svc.download_url = lambda pc, ua="": f"https://cdn.115.test/{pc}"
    svc.export_poll = 0
    assert app.state.strm_sync.run(FULL).strm_created == 3
    app.state.scanner.scan_all()
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    return app, fake, media, c, h


def wait_dupes(app):
    for _ in range(200):
        if not app.state.dupes.job.running:
            return app.state.dupes.job
        time.sleep(0.02)
    raise AssertionError("找重複／刪重複沒有結束")


# ---------------- 網頁、MoviePilot、媒體資訊 ----------------

def make_client(tmp_path: Path, raw=None) -> TestClient:
    raw = raw or {}
    raw.setdefault("server", {})["data_dir"] = str(tmp_path / "data")
    return TestClient(create_app(config_from_dict(raw), scan_on_start=False))


def admin_headers(c: TestClient, name="admin", pw="pw") -> dict:
    r = c.post("/Users/AuthenticateByName", json={"Username": name, "Pw": pw})
    return {"X-Emby-Token": r.json()["AccessToken"]}


def make_config(tmp_path: Path, **mp):
    return config_from_dict(
        {
            "server": {"data_dir": str(tmp_path / "data")},
            "users": [{"name": "admin", "password": "pw", "admin": True}],
            "libraries": [
                {"name": "電影", "type": "movies", "paths": [str(tmp_path / "movies")]},
                {"name": "劇集", "type": "tvshows", "paths": [str(tmp_path / "tv")]},
            ],
            "moviepilot": {"url": "http://mp:3000", "api_token": "tok", **mp},
        }
    )


def touch(path: Path, text: str = "x") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


def wait(cond):
    for _ in range(300):
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("等不到")


PROBE = {
    "format": {"format_name": "matroska,webm", "duration": "3600.5", "size": "9000000000", "bit_rate": "19996000"},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "profile": "Main 10", "width": 3840, "height": 2160,
         "pix_fmt": "yuv420p10le", "color_transfer": "smpte2084", "color_primaries": "bt2020", "color_space": "bt2020nc",
         "avg_frame_rate": "24000/1001", "r_frame_rate": "24000/1001", "display_aspect_ratio": "16:9",
         "sample_aspect_ratio": "1:1", "field_order": "progressive", "disposition": {"default": 1}},
        {"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "channel_layout": "5.1(side)",
         "sample_rate": "48000", "bit_rate": "640000", "disposition": {"default": 0}, "tags": {"language": "chi"}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac", "channels": 2, "channel_layout": "stereo",
         "sample_rate": "48000", "disposition": {"default": 1}, "tags": {"language": "eng", "TITLE": "Stereo"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "width": 1920, "height": 1080,
         "disposition": {"forced": 1}, "tags": {"language": "chi"}},
        {"index": 4, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "chi", "title": "简中"}},
        {"index": 5, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
    ],
    "chapters": [{"start_time": "0.000000", "tags": {"title": "Opening"}}, {"start_time": "95.5005"}],
}
