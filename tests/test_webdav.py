"""WebDAV（只能讀）：要登入、只限管理員時擋一般使用者、只露出設定的 115 資料夾（.. 也跑不出去）、寫入一律拒絕。"""

import base64
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.db import Database
from embyserver.p115 import P115Service

from test_incremental import Fake115


def basic(name, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{name}:{pw}".encode()).decode()}


ADMIN, GUEST = basic("admin", "pw"), basic("guest", "gpw")


def make(tmp_path: Path, **webdav):
    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")},
        "users": [{"name": "admin", "password": "pw", "admin": True}, {"name": "guest", "password": "gpw"}],
        "p115": {"strm": {"tasks": [{"remote": "/影視/電影", "local": str(tmp_path / "media")}]}},
        "webdav": {"enabled": True, **webdav},
    }), scan_on_start=False)
    fake = Fake115()
    svc = P115Service(Database(":memory:"), initial_cookies="UID=1", transport=httpx.MockTransport(fake.handler))
    svc.download_url = lambda pc, ua="": f"https://cdn.115.test/{pc}?ua={ua}"
    app.state.webdav.p115 = svc
    app.state.fake115 = fake
    return app, TestClient(app)


def names(xml: str):
    import re

    return re.findall(r"<D:displayname>(.*?)</D:displayname>", xml)


def test_login_and_admin_only(tmp_path):
    app, c = make(tmp_path)
    assert c.request("PROPFIND", "/dav/").status_code == 401
    assert "Basic" in c.request("PROPFIND", "/dav/").headers["www-authenticate"]
    assert c.request("PROPFIND", "/dav/", headers=basic("admin", "錯的")).status_code == 401
    assert c.request("PROPFIND", "/dav/", headers=GUEST).status_code == 207
    app.state.config.webdav.admin_only = True
    assert c.request("PROPFIND", "/dav/", headers=GUEST).status_code == 403
    assert c.request("PROPFIND", "/dav/", headers=ADMIN).status_code == 207
    # 改了密碼，記住的登入馬上失效
    admin = app.state.auth.authenticate("admin", "pw")
    app.state.auth.update_user(admin["id"], password="new")
    assert c.request("PROPFIND", "/dav/", headers=ADMIN).status_code == 401
    app.state.config.webdav.enabled = False
    assert c.request("PROPFIND", "/dav/", headers=basic("admin", "new")).status_code == 404


def test_only_sync_folders_are_visible(tmp_path):
    _, c = make(tmp_path)
    root = c.request("PROPFIND", "/dav/", headers=ADMIN)
    assert names(root.text) == ["Mi302", "影視"]  # 通往同步目錄的那一層
    assert names(c.request("PROPFIND", "/dav/影視/", headers=ADMIN).text) == ["影視", "電影"]  # 沒有「劇集」
    movies = c.request("PROPFIND", "/dav/影視/電影/", headers=ADMIN)
    assert movies.status_code == 207 and "Old Movie (2001).mkv" in names(movies.text)
    assert "<D:getcontentlength>900000000</D:getcontentlength>" in movies.text
    for path in ("/dav/影視/劇集/", "/dav/影視/劇集/Dark/Dark.S01E01.mkv", "/dav/影視/電影/%2e%2e/劇集/"):
        assert c.request("PROPFIND", path, headers=ADMIN).status_code == 404, path
        assert c.get(path, headers=ADMIN, follow_redirects=False).status_code == 404, path


def test_folder_moved_out_is_not_reachable_by_cached_id(tmp_path, monkeypatch):
    """在 115 上把資料夾移出露出範圍：清單快取過期後，舊路徑和它底下的檔案都找不到，不靠記住的 id 繼續列。"""
    import embyserver.webdav as dav

    app, c = make(tmp_path, root="/影視/劇集")
    fake = app.state.fake115
    assert c.get("/dav/影視/劇集/Dark/Dark.S01E01.mkv", headers=ADMIN, follow_redirects=False).status_code == 302
    fake.dirs[103] = ("Dark", 100)  # 移到 /影視（露出範圍外）
    now = [dav.time.time() + dav.LIST_TTL + 1]
    monkeypatch.setattr(dav.time, "time", lambda: now[0])
    assert c.get("/dav/影視/劇集/Dark/Dark.S01E01.mkv", headers=ADMIN, follow_redirects=False).status_code == 404
    assert c.request("PROPFIND", "/dav/影視/劇集/Dark/", headers=ADMIN).status_code == 404
    assert c.request("PROPFIND", "/dav/影視/劇集/", headers=ADMIN).status_code == 207


def test_play_redirects_and_writes_are_refused(tmp_path):
    _, c = make(tmp_path, root="/影視")  # 指定 115 資料夾：同步任務以外的也看得到
    r = c.get("/dav/影視/劇集/Dark/Dark.S01E01.mkv", headers={**ADMIN, "User-Agent": "Infuse/8"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"https://cdn.115.test/{'b' * 17}?ua=Infuse/8"
    assert c.head("/dav/影視/電影/Old Movie (2001).mkv", headers=ADMIN).headers["content-length"] == "900000000"
    assert c.get("/dav/影視/電影/old movie (2001).mkv", headers=ADMIN, follow_redirects=False).status_code == 404
    assert c.request("PROPFIND", "/dav/", headers=ADMIN).status_code == 207
    for method in ("PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH", "LOCK"):
        r = c.request(method, "/dav/影視/電影/Old Movie (2001).mkv", headers=ADMIN)
        assert r.status_code == 405, method
    assert c.request("DELETE", "/dav/影視/電影/Old Movie (2001).mkv").status_code == 401
