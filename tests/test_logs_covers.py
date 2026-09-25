"""日誌頁 API、媒體庫封面上傳（MoviePilot 封面插件）、MoviePilot 通知與同步後的部分掃描。"""

import base64
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from embyserver import logs
from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.strm_sync import SyncResult

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def touch(path: Path, text: str = "http://x/a.mkv") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make(tmp_path: Path, **mp):
    touch(tmp_path / "movies" / "A (2020)" / "A (2020).strm")
    touch(tmp_path / "tv" / "Show (2021)" / "Season 1" / "Show.S01E01.strm")
    config = config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")},
        "users": [{"name": "admin", "password": "pw", "admin": True}],
        "libraries": [
            {"name": "電影", "type": "movies", "paths": [str(tmp_path / "movies")]},
            {"name": "劇集", "type": "tvshows", "paths": [str(tmp_path / "tv")]},
        ],
        "moviepilot": {"url": "http://mp:3000", "api_token": "tok", **mp},
    })
    app = create_app(config, scan_on_start=False)
    app.state.scanner.scan_all()
    c = TestClient(app)
    token = c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]
    return app, c, {"X-Emby-Token": token}


def library_id(c, h, name: str) -> str:
    return next(f["Id"] for f in c.get("/emby/Library/VirtualFolders/Query", headers=h).json()["Items"] if f["Name"] == name)


def test_cover_plugin_uploads_library_cover(tmp_path: Path):
    app, c, h = make(tmp_path)
    key = c.post("/web/api/apikeys", json={"name": "MoviePilot"}, headers=h).json()["key"]
    lib = library_id(c, h, "電影")

    # 外掛的做法：POST base64 文字，Content-Type 是圖片格式
    r = c.post(f"/emby/Items/{lib}/Images/Primary", params={"api_key": key},
               content=base64.b64encode(PNG), headers={"Content-Type": "image/png"})
    assert r.status_code == 204
    views = c.get("/Users/x/Views", headers=h).json()["Items"]
    tag = next(v for v in views if v["Name"] == "電影")["ImageTags"]["Primary"]
    img = c.get(f"/emby/Items/{lib}/Images/Primary", params={"tag": tag})
    assert img.status_code == 200 and img.content == PNG

    # 重新掃描不會把上傳的封面蓋掉；資料夾裡有 poster 也是上傳的優先
    (tmp_path / "movies" / "poster.jpg").write_bytes(b"\xff\xd8jpg")
    app.state.scanner.scan_all()
    assert c.get(f"/Items/{lib}/Images/Primary").content == PNG
    status = c.get("/web/api/scan", headers=h).json()["libraries"]
    assert next(s for s in status if s["name"] == "電影")["custom_cover"] is True

    # 改回預設：用資料夾裡的圖
    assert c.delete(f"/Items/{lib}/Images/Primary", headers=h).status_code == 204
    assert c.get(f"/Items/{lib}/Images/Primary").content == b"\xff\xd8jpg"

    # 網頁上傳原始檔案、不支援的格式
    assert c.post(f"/Items/{lib}/Images/Primary", content=PNG, headers={**h, "Content-Type": "image/png"}).status_code == 204
    bad = c.post(f"/Items/{lib}/Images/Primary", content=b"hello", headers={**h, "Content-Type": "image/png"})
    assert bad.status_code == 400
    # 沒有權限
    assert c.post(f"/Items/{lib}/Images/Primary", content=PNG).status_code == 401


def test_cover_plugin_item_query(tmp_path: Path):
    app, c, h = make(tmp_path)
    lib = library_id(c, h, "劇集")
    r = c.get("/emby/Items/", headers=h, params={
        "ParentId": lib, "SortBy": "Random", "Limit": 50, "StartIndex": 0,
        "IncludeItemTypes": "Movie,Series", "Recursive": "True", "SortOrder": "Descending",
    })
    assert [i["Name"] for i in r.json()["Items"]] == ["Show"]


def test_media_updated_scans_only_given_paths(tmp_path: Path):
    app, c, h = make(tmp_path, path_mappings=[{"from": str(tmp_path), "to": "/mp"}])
    calls = []
    app.state.scanner.scan_paths = lambda paths: calls.append(list(paths))
    app.state.scanner.scan_all = lambda: calls.append("all")
    body = {"Updates": [{"Path": "/mp/movies/B (2021)/B (2021).mkv", "UpdateType": "Created"}]}
    assert c.post("/emby/Library/Media/Updated", json=body, headers=h).status_code == 204
    assert c.post("/emby/Library/Media/Updated", headers=h).status_code == 204
    assert calls == [[str(tmp_path / "movies" / "B (2021)" / "B (2021).mkv")], "all"]

    movie = next(i for i in c.get("/Items", headers=h, params={"IncludeItemTypes": "Movie", "Recursive": "true"}).json()["Items"])
    assert c.post(f"/Items/{movie['Id']}/Refresh", headers=h).status_code == 204
    assert calls[-1] == [str(tmp_path / "movies" / "A (2020)" / "A (2020).strm")]


def test_after_sync_scans_changed_paths(tmp_path: Path):
    app, c, h = make(tmp_path)
    calls = []
    app.state.scanner.scan_paths = lambda paths: calls.append(list(paths))
    app.state.scanner.scan_all = lambda: calls.append("all")
    app.state.moviepilot.scrape = lambda paths, source: None
    app.state.strm_sync.on_done(SyncResult(changed=["/x/a.strm"], new_files=["/x/a.strm"]))
    app.state.strm_sync.on_done(SyncResult())  # 沒有變動就不掃
    assert calls == [["/x/a.strm"]]


def test_web_scan_requests(tmp_path: Path):
    app, c, h = make(tmp_path)
    assert c.post("/web/api/scan", json={"library": "不存在"}, headers=h).status_code == 400
    assert c.post("/web/api/scan", json={"path": "/etc"}, headers=h).status_code == 400
    assert c.post("/web/api/scan", json={"path": str(tmp_path / "tv")}, headers=h).status_code == 204
    assert c.post("/web/api/scan", json={"library": "劇集"}, headers=h).status_code == 204


def test_logs_api_filters_and_redacts(tmp_path: Path):
    app, c, h = make(tmp_path)
    root = logging.getLogger()
    old = root.level
    root.setLevel(logging.DEBUG)
    try:
        logging.getLogger("embyserver.test").warning("找不到 %s", "某部片")
        logging.getLogger("embyserver.test").info("一般訊息")
        c.get("/emby/System/Info/Public", params={"api_key": "secret123"})
    finally:
        root.setLevel(old)
    last = c.get("/web/api/logs", headers=h, params={"level": "WARNING"}).json()
    assert any(i["message"] == "找不到 某部片" for i in last["items"])
    assert all(i["level"] in ("WARNING", "ERROR", "CRITICAL") for i in last["items"])
    found = c.get("/web/api/logs", headers=h, params={"level": "DEBUG", "q": "system/info"}).json()["items"]
    assert found and "secret123" not in found[-1]["message"] and "api_key=***" in found[-1]["message"]
    # 只拿比 after 新的
    assert c.get("/web/api/logs", headers=h, params={"after": last["last"], "level": "WARNING"}).json()["items"] == []
    dl = c.get("/web/api/logs/download", headers=h)
    assert dl.status_code == 200 and "找不到 某部片" in dl.text
    assert c.get("/web/api/logs").status_code == 401


def test_log_level_setting(tmp_path: Path):
    app, c, h = make(tmp_path)
    root = logging.getLogger()
    old = root.level
    try:
        r = c.put("/web/api/settings", json={"server": {"log_level": "debug"}}, headers=h)
        assert r.status_code == 200 and r.json()["server"]["log_level"] == "debug"
        assert root.level == logging.DEBUG
        assert c.put("/web/api/settings", json={"server": {"log_level": "weird"}}, headers=h).json()["server"]["log_level"] == "info"
        assert root.level == logging.INFO
    finally:
        root.setLevel(old)


def test_setup_writes_log_file(tmp_path: Path):
    path = logs.setup(tmp_path / "data", "info")
    try:
        logging.getLogger("embyserver.test").warning("寫進檔案")
        for h in logging.getLogger().handlers:
            h.flush()
        assert path and "寫進檔案" in path.read_text(encoding="utf-8")
        assert logs.files()[0]["name"] == "mi302.log"
    finally:
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, logging.FileHandler):
                root.removeHandler(h)
                h.close()
        logs._file_path = None
        root.setLevel(logging.WARNING)
