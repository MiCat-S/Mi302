"""115 雲下載（離線下載）：開放平台和 cookie 兩條路送出去的內容，刪除時「連同檔案」只在勾了才帶，清除只能清已完成、已失敗。"""

import base64
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from embyserver.db import Database
from embyserver.offline115 import OfflineDownloads, OfflineError, clean_urls
from embyserver.p115 import P115Service

MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
HASH = "0123456789abcdef0123456789abcdef01234567"


class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()} if request.content else {}
        self.calls.append((request.url.host, request.url.path, dict(request.url.params), form, request.headers))
        path = request.url.path
        if path == "/files/getid":
            return httpx.Response(200, json={"state": True, "id": "777" if request.url.params["path"] == "/下載" else "0"})
        if path == "/lixianssp/":
            return httpx.Response(200, json={"state": True, "result": [
                {"state": True, "info_hash": HASH, "url": MAGNET, "name": "電影"},
                {"state": False, "errcode": 10008, "error_msg": "任務已存在", "url": "ed2k://|file|a|1|x|/"}]})
        if path == "/web/":
            ac = request.url.params.get("ac") or form.get("ac")
            if ac == "task_lists":
                return httpx.Response(200, json={"state": True, "page": 1, "page_count": 1, "count": 2, "tasks": [
                    {"info_hash": HASH, "name": "電影", "size": 1000, "percentDone": 100, "status": 2, "file_id": "9",
                     "wp_path_id": "777", "add_time": 1, "last_update": 2},
                    {"info_hash": "f" * 40, "name": "劇", "size": 10, "percentDone": 42.5, "status": 1}]})
            if ac == "get_quota_info":
                return httpx.Response(200, json={"state": True, "surplus": 1490, "count": 1500, "used": 10})
            return httpx.Response(200, json={"state": True})
        if path.startswith("/open/offline/"):
            if path.endswith("get_task_list"):
                return httpx.Response(200, json={"state": True, "data": {"page": 1, "page_count": 1, "count": 1, "tasks": [
                    {"info_hash": HASH, "name": "失敗的", "percentDone": 0, "status": -1}]}})
            if path.endswith("add_task_urls"):
                return httpx.Response(200, json={"state": True, "data": [{"state": True, "info_hash": HASH, "url": MAGNET}]})
            return httpx.Response(200, json={"state": True, "data": {}})
        return httpx.Response(404)


def service(open_platform=False):
    fake = Fake()
    db = Database(":memory:")
    if open_platform:  # 已經授權開放平台（直接放 token，不走掃碼）
        db.set_meta("p115_open_token", json.dumps({"access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 7200,
                                                   "app_id": "app1"}))
    p115 = P115Service(db, initial_cookies="UID=1_A1_2; CID=x; SEID=y", transport=httpx.MockTransport(fake))
    return OfflineDownloads(p115), fake


def test_clean_urls():
    ok, bad = clean_urls(f"{MAGNET}\n\n  {HASH.upper()}  \nhttps://a.test/x.mkv\n{MAGNET}\n不是連結\nftp://f.test/1")
    assert ok == [MAGNET, f"magnet:?xt=urn:btih:{HASH.upper()}", "https://a.test/x.mkv", "ftp://f.test/1"]
    assert bad == ["不是連結"]


def test_cookie_add_list_and_quota(monkeypatch):
    import p115cipher

    monkeypatch.setattr(p115cipher, "rsa_encrypt", lambda b: base64.b64encode(b))  # 看得到送了什麼
    off, fake = service()
    r = off.add(f"{MAGNET}\ned2k://|file|a|1|x|/\n亂打的", "/下載")
    assert (r["added"], r["rejected"], r["folder"], r["via"]) == (1, ["亂打的"], "/下載", "cookie")
    assert r["results"][1] == {"url": "ed2k://|file|a|1|x|/", "ok": False, "name": "", "message": "任務已存在"}
    host, path, _, form, headers = fake.calls[-1]
    sent = json.loads(base64.b64decode(form["data"]))
    assert (host, path) == ("clouddownload.115.com", "/lixianssp/") and "115wangpan_android" in headers["user-agent"]
    assert sent == {"url[0]": MAGNET, "url[1]": "ed2k://|file|a|1|x|/", "wp_path_id": "777", "ac": "add_task_urls",
                    "app_ver": "36.2.28"}
    listing = off.tasks()
    assert [(t["name"], t["state"], t["percent"]) for t in listing["tasks"]] == [("電影", "done", 100.0), ("劇", "downloading", 42.5)]
    assert listing["tasks"][0]["folder_id"] == "777" and listing["quota"] == {"left": 1490, "total": 1500, "used": 10}
    with pytest.raises(OfflineError, match="沒有可以下載的連結"):
        off.add("只有文字", "")


def test_delete_only_takes_files_when_asked():
    off, fake = service()
    off.delete([HASH, HASH, "bad hash!"], with_files=False)
    form = fake.calls[-1][3]
    assert form == {"hash[0]": HASH, "ac": "task_del", "flag": "0"}  # 預設不刪下載好的檔案
    off.delete([HASH], with_files=True)
    assert fake.calls[-1][3]["flag"] == "1"
    with pytest.raises(OfflineError):
        off.delete(["bad hash!"])


def test_clear_only_done_or_failed():
    off, fake = service()
    off.clear("done")
    assert fake.calls[-1][3] == {"ac": "task_clear", "flag": "0"}
    off.clear("failed")
    assert fake.calls[-1][3]["flag"] == "2"
    for what in ("all", "running", "4", ""):  # 清全部會取消下載中的，4、5 會連檔案一起刪
        with pytest.raises(OfflineError):
            off.clear(what)


def test_open_platform_is_used_when_authorized():
    off, fake = service(open_platform=True)
    r = off.add(MAGNET, "")
    host, path, _, form, headers = fake.calls[-1]
    assert (host, path, form, r["via"]) == ("proapi.115.com", "/open/offline/add_task_urls", {"urls": MAGNET}, "open")
    assert headers["authorization"] == "Bearer at"
    assert off.tasks()["tasks"][0]["state"] == "failed"
    off.delete([HASH], with_files=True)
    assert fake.calls[-1][1:4:2] == ("/open/offline/del_task", {"info_hash": HASH, "del_source_file": "1"})
