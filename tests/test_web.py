"""網頁管理 API：首次設定、媒體庫、使用者、進階設定、選資料夾。"""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict, load_config


def make_client(tmp_path: Path, raw=None) -> TestClient:
    raw = raw or {}
    raw.setdefault("server", {})["data_dir"] = str(tmp_path / "data")
    return TestClient(create_app(config_from_dict(raw), scan_on_start=False))


def admin_headers(c: TestClient, name="admin", pw="pw") -> dict:
    r = c.post("/Users/AuthenticateByName", json={"Username": name, "Pw": pw})
    return {"X-Emby-Token": r.json()["AccessToken"]}


def wait_scan(c):
    for _ in range(50):
        if not c.app.state.scanner.scanning:
            return
        time.sleep(0.05)


def test_starts_without_config_file(tmp_path: Path):
    cfg = load_config(str(tmp_path / "nope.yaml"))
    assert cfg.users == [] and cfg.libraries == []


def test_first_run_setup(tmp_path: Path):
    c = make_client(tmp_path)
    assert c.get("/web").status_code == 200
    assert c.get("/web/api/setup").json() == {"needed": True}
    assert c.post("/web/api/setup", json={"name": "cat", "password": ""}).status_code == 400
    token = c.post("/web/api/setup", json={"name": "cat", "password": "pw"}).json()["token"]
    assert c.get("/web/api/settings", headers={"X-Emby-Token": token}).status_code == 200
    # 設定過之後不能再用這個端點新增管理員
    assert c.get("/web/api/setup").json() == {"needed": False}
    assert c.post("/web/api/setup", json={"name": "evil", "password": "x"}).status_code == 403


def test_libraries_from_web_are_scanned_and_persist(tmp_path: Path):
    movie = tmp_path / "movies" / "Inception (2010)"
    movie.mkdir(parents=True)
    (movie / "Inception (2010).strm").write_text("http://x/a.mkv")
    raw = {"users": [{"name": "admin", "password": "pw", "admin": True}]}
    c = make_client(tmp_path, raw)
    h = admin_headers(c)
    assert c.put("/web/api/settings", json={"libraries": [{"name": "", "paths": ["/x"]}]}, headers=h).status_code == 400
    libs = [{"name": "電影", "type": "movies", "paths": [str(tmp_path / "movies")]}]
    r = c.put("/web/api/settings", json={"libraries": libs, "server": {"name": "家"}}, headers=h)
    assert r.status_code == 200 and r.json()["libraries"] == libs
    wait_scan(c)
    scan = c.get("/web/api/scan", headers=h).json()
    lib = scan["libraries"][0]
    assert (lib["name"], lib["count"], lib["missing"], lib["custom_cover"]) == ("電影", 1, [], False)
    assert c.get("/System/Info/Public").json()["ServerName"] == "家"

    # 沒有設定檔時（例如測試直接給設定），設定只在記憶體裡
    assert c.app.state.config.path is None


def test_settings_update_live_objects(tmp_path: Path):
    c = make_client(tmp_path, {"users": [{"name": "admin", "password": "pw", "admin": True}]})
    h = admin_headers(c)
    body = {
        "p115": {"app": "tv", "open_app_id": "123", "strm": {"interval": "30", "delete_stale": True}},
        "redirect": {"path_rules": [{"from": "/mnt", "to": "http://a"}], "require_auth": True},
    }
    r = c.put("/web/api/settings", json=body, headers=h).json()
    assert r["p115"]["strm"]["interval"] == 30 and r["p115"]["strm"]["delete_stale"] is True
    st = c.app.state
    assert st.strm_sync.cfg.interval == 30
    assert st.redirector.config.path_rules[0].target == "http://a" and st.redirector.config.require_auth
    assert st.p115.app == "tv" and st.p115.open.default_app_id == "123"
    # 型別錯誤時不留下改一半的設定
    bad = {"p115": {"strm": {"interval": "abc", "min_size_mb": 5}}}
    assert c.put("/web/api/settings", json=bad, headers=h).status_code == 400
    assert st.config.p115.strm.min_size_mb == 0


def test_user_management(tmp_path: Path):
    c = make_client(tmp_path, {"users": [{"name": "admin", "password": "pw", "admin": True}]})
    h = admin_headers(c)
    assert c.post("/web/api/users", json={"name": "kid", "password": ""}, headers=h).status_code == 400
    uid = c.post("/web/api/users", json={"name": "kid", "password": "k"}, headers=h).json()["id"]
    assert c.put(f"/web/api/users/{uid}", json={"password": ""}, headers=h).status_code == 400
    assert c.post("/Users/AuthenticateByName", json={"Username": "kid", "Pw": ""}).status_code == 401
    assert c.post("/web/api/users", json={"name": "KID", "password": "k"}, headers=h).status_code == 400
    # 一般使用者不能進管理 API
    assert c.get("/web/api/users", headers=admin_headers(c, "kid", "k")).status_code == 403
    c.put(f"/web/api/users/{uid}", json={"password": "new"}, headers=h)
    assert c.post("/Users/AuthenticateByName", json={"Username": "kid", "Pw": "new"}).status_code == 200
    admin_id = next(u["id"] for u in c.get("/web/api/users", headers=h).json() if u["name"] == "admin")
    assert c.delete(f"/web/api/users/{admin_id}", headers=h).status_code == 400
    assert c.put(f"/web/api/users/{admin_id}", json={"admin": False}, headers=h).status_code == 400
    assert c.delete(f"/web/api/users/{uid}", headers=h).status_code == 204

    # 網頁改過的密碼，重新啟動時不會被設定檔蓋回去
    c.put(f"/web/api/users/{admin_id}", json={"password": "changed"}, headers=h)
    c2 = make_client(tmp_path, {"users": [{"name": "admin", "password": "pw", "admin": True}]})
    assert c2.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "changed"}).status_code == 200


def test_browse_local(tmp_path: Path):
    (tmp_path / "media" / "movies").mkdir(parents=True)
    (tmp_path / "media" / ".hidden").mkdir()
    c = make_client(tmp_path, {"users": [{"name": "admin", "password": "pw", "admin": True}]})
    h = admin_headers(c)
    r = c.get("/web/api/browse", params={"path": str(tmp_path / "media")}, headers=h).json()
    assert r["dirs"] == ["movies"] and r["parent"] == str(tmp_path)
    assert c.get("/web/api/browse", params={"path": str(tmp_path / "nope")}, headers=h).status_code == 400
    assert c.get("/web/api/browse", params={"path": "/"}).status_code == 401


def test_scan_status_reports_progress(tmp_path: Path):
    movie = tmp_path / "movies" / "A (2020)"
    movie.mkdir(parents=True)
    (movie / "A (2020).strm").write_text("http://x/a.mkv")
    raw = {"users": [{"name": "admin", "password": "pw", "admin": True}],
           "libraries": [{"name": "電影", "type": "movies", "paths": [str(tmp_path / "movies")]}]}
    c = make_client(tmp_path, raw)
    c.app.state.scanner.scan_all()
    c.app.state.scanner.scan_all()
    s = c.get("/web/api/scan", headers=admin_headers(c)).json()
    assert s["scanning"] is False and s["progress"] == {"done": 1, "total": 1, "item": ""}


def test_api_key_cannot_open_admin_api(tmp_path: Path):
    """API 金鑰是給 MoviePilot 呼叫 Emby API 的；設定（含 MoviePilot 密碼）、備份、帳號要用帳號登入。"""
    c = make_client(tmp_path, {"users": [{"name": "admin", "password": "pw", "admin": True}]})
    key = c.post("/web/api/apikeys", json={"name": "mp"}, headers=admin_headers(c)).json()["key"]
    k = {"X-Emby-Token": key}
    assert c.get("/web/api/settings", headers=k).status_code == 403
    assert c.get("/web/api/backups", headers=k).status_code == 403
    assert c.get("/p115/status", headers=k).status_code == 403
    assert c.post("/Library/Refresh", headers=k).status_code == 204  # Emby API 照常可用


def test_config_tolerates_unknown_keys_and_float_fields(tmp_path: Path):
    from embyserver import settings
    from embyserver.config import config_from_dict
    from embyserver.db import Database

    cfg = config_from_dict({"server": {"log_leve": "debug"}, "p115": {"strm": {"old_key": 1}}})  # 打錯字不能讓程式起不來
    assert cfg.server.log_level == "info"
    settings.save(Database(":memory:"), cfg, {"p115": {"strm": {"min_size_mb": "0.5"}}, "moviepilot": {"timeout": "12.5"}})
    assert cfg.p115.strm.min_size_mb == 0.5 and cfg.moviepilot.timeout == 12.5  # 預設值是 0、300，型別看宣告


def test_client_disconnect_is_not_logged_as_error(tmp_path: Path, monkeypatch, caplog):
    """瀏覽器在請求送完前就斷線（例如按了按鈕就重新整理）：記一行 INFO，不要整段錯誤堆疊。"""
    import logging

    from starlette.requests import ClientDisconnect

    from embyserver.routes import web

    c = make_client(tmp_path, {"users": [{"name": "admin", "password": "pw", "admin": True}]})
    h = admin_headers(c)

    async def gone(request):
        raise ClientDisconnect()

    monkeypatch.setattr(web, "_body", gone)
    with caplog.at_level(logging.INFO):
        r = c.post("/web/api/mediainfo/probe", json={}, headers=h)
    assert r.status_code == 499
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert any("請求送完之前就斷了" in rec.getMessage() for rec in caplog.records)


def test_non_object_json_body_is_400(tmp_path: Path):
    """送來的 JSON 不是物件（例如 [1]、"x"、3）：回 400，不是 .get 炸掉的 500。"""
    c = make_client(tmp_path, {"users": [{"name": "admin", "password": "pw", "admin": True}]})
    h = admin_headers(c)
    for method, path in (
        ("post", "/web/api/users"), ("put", "/web/api/settings"), ("post", "/web/api/scan"),
        ("post", "/web/api/intro/clear"), ("post", "/web/api/apikeys"), ("post", "/web/api/dupes/scan"),
        ("post", "/web/api/moviepilot/fill"), ("post", "/web/api/moviepilot/reorganize/execute"),
    ):
        for body in ("[1]", '"x"', "3"):
            r = c.request(method, path, content=body, headers={**h, "Content-Type": "application/json"})
            assert r.status_code == 400, (path, body, r.status_code, r.text)
    # 沒登入時照樣先擋權限，不先看內容
    assert c.post("/web/api/intro/clear", content="[1]").status_code == 401


def test_blocking_endpoints_run_in_threadpool(tmp_path: Path):
    """會寫檔、寫資料庫的端點寫成一般 def，FastAPI 放進執行緒池，不佔住事件迴圈。"""
    import inspect

    from embyserver.routes import items, web

    for endpoint in (web.intro_clear, web.create_api_key, items.upload_image):
        assert not inspect.iscoroutinefunction(endpoint), endpoint.__name__
    c = make_client(tmp_path, {"users": [{"name": "admin", "password": "pw", "admin": True}]})
    h = admin_headers(c)
    assert c.post("/web/api/intro/clear", json={}, headers=h).json() == {"removed": 0}
    key = c.post("/web/api/apikeys", json={"name": "MP"}, headers=h).json()
    assert key["name"] == "MP" and [k["key"] for k in c.get("/web/api/apikeys", headers=h).json()] == [key["key"]]
