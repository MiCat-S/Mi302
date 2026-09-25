"""網頁設定與 config.yaml 同步。"""

import json
import os
import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from embyserver import config_file
from embyserver.app import create_app
from embyserver.config import config_from_dict, load_config
from embyserver.db import Database


def write_yaml(path: Path, raw: dict) -> None:
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")


def start(path: Path) -> TestClient:
    return TestClient(create_app(load_config(str(path)), scan_on_start=False))


def login(c: TestClient) -> dict:
    token = c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]
    return {"X-Emby-Token": token}


def base(tmp_path: Path) -> dict:
    return {
        "server": {"name": "Mi302", "data_dir": str(tmp_path / "data"), "port": 8097},
        "users": [{"name": "admin", "password": "pw", "admin": True}],
    }


def test_render_round_trips_tricky_values(tmp_path: Path):
    raw = base(tmp_path)
    raw["libraries"] = [{"name": "電影: 4K #1", "type": "movies", "paths": ["/media/電影 'A'", '/b"c']}]
    raw["p115"] = {"cookies": "UID=1; CID=2", "strm": {"tasks": [{"remote": "/影視/電影", "local": "/media/115"}], "interval": 5}}
    raw["moviepilot"] = {"url": "http://mp:3000", "api_token": "yes", "path_mappings": [{"from": "/media", "to": "/mnt/m"}]}
    raw["redirect"] = {"path_rules": [{"from": "/mnt/115", "to": "http://alist:5244/d/115"}]}
    raw["api_keys"] = ["k1"]
    cfg = config_from_dict(raw)
    out = tmp_path / "config.yaml"
    config_file.write(cfg, str(out))
    again = load_config(str(out))
    for part in ("server", "users", "libraries", "p115", "moviepilot", "redirect", "api_keys"):
        assert getattr(again, part) == getattr(cfg, part), part
    assert "# 網頁" in out.read_text(encoding="utf-8")  # 有說明註解


def test_web_changes_are_written_to_config_file(tmp_path: Path):
    path = tmp_path / "config.yaml"
    write_yaml(path, base(tmp_path))
    c = start(path)
    h = login(c)
    libs = [{"name": "電影", "type": "movies", "paths": [str(tmp_path / "movies")]}]
    body = {"libraries": libs, "moviepilot": {"url": "http://mp:3000/", "api_token": "tok"},
            "p115": {"strm": {"interval": 5}}}
    assert c.put("/web/api/settings", json=body, headers=h).status_code == 200
    tasks = c.put("/p115/strm/tasks", json=[{"remote": "影視", "local": str(tmp_path / "movies/115")}], headers=h)
    assert tasks.status_code == 200

    saved = load_config(str(path))
    assert [l.name for l in saved.libraries] == ["電影"]
    assert saved.moviepilot.url == "http://mp:3000" and saved.moviepilot.api_token == "tok"
    assert saved.p115.strm.interval == 5 and saved.p115.strm.tasks[0].remote == "/影視"
    assert saved.users[0].name == "admin" and saved.server.port == 8097  # 沒改的保留
    assert (tmp_path / "config.yaml.bak").exists()
    # 資料庫裡不再另外存一份
    assert c.app.state.db.get_meta("web_settings") is None

    # 重新啟動後讀到一樣的設定
    c2 = start(path)
    assert c2.app.state.config.moviepilot.url == "http://mp:3000"
    assert [t.remote for t in c2.app.state.strm_sync.tasks] == ["/影視"]


def test_manual_file_edits_show_up_without_restart(tmp_path: Path):
    path = tmp_path / "config.yaml"
    write_yaml(path, base(tmp_path))
    c = start(path)
    h = login(c)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["moviepilot"] = {"url": "http://edited:3000"}
    raw["libraries"] = [{"name": "劇集", "type": "tvshows", "paths": [str(tmp_path / "tv")]}]
    write_yaml(path, raw)
    os.utime(path, (time.time() + 5, time.time() + 5))  # 確保修改時間不同
    s = c.get("/web/api/settings", headers=h).json()
    assert s["moviepilot"]["url"] == "http://edited:3000" and s["file_error"] == ""
    assert c.app.state.moviepilot.cfg.url == "http://edited:3000"  # 執行中的元件也拿到新值
    assert [l["name"] for l in s["libraries"]] == ["劇集"]
    assert s["config_path"] == str(path.resolve())

    # 網頁再改別的，手動改的內容不會被蓋掉
    c.put("/web/api/settings", json={"server": {"name": "家"}}, headers=h)
    saved = load_config(str(path))
    assert saved.server.name == "家" and saved.moviepilot.url == "http://edited:3000"


def test_broken_file_is_reported_and_not_overwritten(tmp_path: Path):
    path = tmp_path / "config.yaml"
    write_yaml(path, base(tmp_path))
    c = start(path)
    h = login(c)
    path.write_text("server: [這裡寫錯了\n", encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    s = c.get("/web/api/settings", headers=h).json()
    assert "設定檔有錯" in s["file_error"] and s["server"]["name"] == "Mi302"
    r = c.put("/web/api/settings", json={"server": {"name": "x"}}, headers=h)
    assert r.status_code == 400 and "設定檔有錯" in r.text
    assert path.read_text(encoding="utf-8") == "server: [這裡寫錯了\n"


def test_old_database_settings_move_into_config_file(tmp_path: Path):
    path = tmp_path / "config.yaml"
    raw = base(tmp_path)
    write_yaml(path, raw)
    db = Database(tmp_path / "data" / "library.db")
    db.set_meta("web_settings", json.dumps({"moviepilot": {"url": "http://old:3000"}, "server": {"name": "舊"}}))
    db.set_meta("p115_strm_tasks", json.dumps([{"remote": "/影視", "local": "/media/115"}]))
    db.conn.close()

    c = start(path)
    saved = load_config(str(path))
    assert saved.moviepilot.url == "http://old:3000" and saved.server.name == "舊"
    assert saved.p115.strm.tasks[0].local == "/media/115"
    assert c.app.state.db.get_meta("web_settings") is None and c.app.state.db.get_meta("p115_strm_tasks") is None


def test_config_file_created_when_missing(tmp_path: Path):
    path = tmp_path / "conf" / "config.yaml"
    cfg = load_config(str(path))
    cfg.server.data_dir = str(tmp_path / "data")
    create_app(cfg, scan_on_start=False)
    assert load_config(str(path)).server.data_dir == str(tmp_path / "data")
