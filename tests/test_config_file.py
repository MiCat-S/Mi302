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
    if os.name != "nt":  # 設定檔有密碼、API 令牌：改寫後只給執行 Mi302 的帳號讀
        assert {f.stat().st_mode & 0o777 for f in (path, tmp_path / "config.yaml.bak")} == {0o600}
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


def test_task_folders_must_be_absolute_and_disjoint():
    import pytest

    from embyserver.settings import SettingsError, _tasks

    with pytest.raises(SettingsError, match="完整路徑"):
        _tasks([{"remote": "/影視", "local": "media"}])
    with pytest.raises(SettingsError, match="互相包含"):
        _tasks([{"remote": "/影視", "local": "/media"}, {"remote": "/影視/劇集", "local": "/media/劇集/"}])
    with pytest.raises(SettingsError, match="互相包含"):
        _tasks([{"remote": "/a", "local": "/media"}, {"remote": "/b", "local": "/media"}])
    assert len(_tasks([{"remote": "/a", "local": "/media/a"}, {"remote": "/b", "local": "/media/b"}])) == 2


def test_field_lists_stay_in_sync():
    """新欄位要改三處：config 的 dataclass、settings 的 *_FIELDS（網頁能改的）、config_file.render（寫回設定檔）。
    漏改一處這裡就會紅。網頁故意不開放的欄位列在 WEB_ONLY_IN_FILE。"""
    from dataclasses import fields

    from embyserver import settings
    from embyserver.config import (Config, MediaInfoConfig, MoviePilotConfig, P115Config, P115StrmConfig,
                                   RedirectConfig, ServerConfig, WebDAVConfig)

    sections = {  # 設定檔裡的位置 → (dataclass, 網頁能改的欄位)
        ("server",): (ServerConfig, settings.SERVER_FIELDS),
        ("p115",): (P115Config, settings.P115_FIELDS),
        ("p115", "strm"): (P115StrmConfig, settings.STRM_FIELDS),
        ("moviepilot",): (MoviePilotConfig, settings.MOVIEPILOT_FIELDS),
        ("mediainfo",): (MediaInfoConfig, settings.MEDIAINFO_FIELDS),
        ("webdav",): (WebDAVConfig, settings.WEBDAV_FIELDS),
        ("redirect",): (RedirectConfig, settings.REDIRECT_FIELDS),
    }
    # 網頁不能改、只在設定檔裡的（或網頁用別的方式改的：任務、路徑對應、路徑替換）
    web_only_in_file = {
        ("server",): {"host", "port", "data_dir"},
        ("p115",): {"cookies", "timeout", "strm"},
        ("p115", "strm"): {"tasks"},
        ("moviepilot",): {"path_mappings"},
        ("mediainfo",): set(),
        ("webdav",): set(),
        ("redirect",): {"path_rules"},
    }
    rendered = yaml.safe_load(config_file.render(Config()))
    for where, (cls, web_fields) in sections.items():
        names = {f.name for f in fields(cls)}
        assert len(set(web_fields)) == len(web_fields), where
        assert set(web_fields) | web_only_in_file[where] == names, (where, names ^ (set(web_fields) | web_only_in_file[where]))
        section = rendered
        for key in where:
            section = section[key]
        assert set(section) == names, (where, set(section) ^ names)
    exported = settings.export_settings(Config())
    assert set(exported["server"]) == set(settings.SERVER_FIELDS)
    assert set(exported["p115"]["strm"]) == set(settings.STRM_FIELDS) | {"tasks"}


def touch_later(path: Path) -> None:
    os.utime(path, (time.time() + 5, time.time() + 5))  # 確保修改時間不同，網頁會重新讀


def test_bad_file_settings_are_checked_at_startup(tmp_path: Path):
    """手動改的設定檔沒經過網頁的檢查：啟動時補做。有錯的同步任務先不用（相對路徑的 strm 會寫到工作目錄），
    其他錯只提示；照常啟動，網頁上方看得到。"""
    path = tmp_path / "config.yaml"
    raw = base(tmp_path)
    good = str(tmp_path / "media" / "115")
    raw["p115"] = {"strm": {"tasks": [{"remote": "/a", "local": "relative/dir"}, {"remote": "/b", "local": good},
                                      {"remote": "/c", "local": good + "/sub"}]}}
    raw["moviepilot"] = {"url": "mp:3000"}
    write_yaml(path, raw)
    c = start(path)
    h = login(c)
    assert [t.remote for t in c.app.state.strm_sync.tasks] == ["/b"]
    s = c.get("/web/api/settings", headers=h).json()
    assert [t["remote"] for t in s["p115"]["strm"]["tasks"]] == ["/b"]
    assert len(s["problems"]) == 3 and "/a → relative/dir" in s["problems"][0] and "完整路徑" in s["problems"][0]
    assert "互相包含" in s["problems"][1] and "http://" in s["problems"][2]

    # 修正設定檔：重新整理網頁就套用，提示不見
    raw["p115"]["strm"]["tasks"] = [{"remote": "/a", "local": str(tmp_path / "a")}, {"remote": "/b", "local": good}]
    raw["moviepilot"] = {"url": "http://mp:3000"}
    write_yaml(path, raw)
    touch_later(path)
    s = c.get("/web/api/settings", headers=h).json()
    assert s["problems"] == [] and [t.remote for t in c.app.state.strm_sync.tasks] == ["/a", "/b"]


def test_saving_on_the_web_drops_bad_tasks_from_file(tmp_path: Path):
    path = tmp_path / "config.yaml"
    raw = base(tmp_path)
    raw["p115"] = {"strm": {"tasks": [{"remote": "/a", "local": "relative"}, {"remote": "/b", "local": str(tmp_path / "b")}]}}
    write_yaml(path, raw)
    c = start(path)
    h = login(c)
    r = c.put("/web/api/settings", json={"server": {"name": "家"}}, headers=h)
    assert r.status_code == 200 and r.json()["problems"] == []
    assert [t.remote for t in load_config(str(path)).p115.strm.tasks] == ["/b"]


def test_deleted_config_user_does_not_come_back(tmp_path: Path):
    """設定檔 users 裡的帳號在網頁上刪掉：一起從設定檔拿掉，重新啟動不會再建立回來。"""
    path = tmp_path / "config.yaml"
    raw = base(tmp_path)
    raw["users"] += [{"name": "Kid", "password": "pw2", "admin": False}, {"name": "ADMIN", "password": "x", "admin": True}]
    raw["p115"] = {"strm": {"tasks": [{"remote": "/a", "local": "relative"}]}}  # 刪帳號會重寫設定檔，這個一起拿掉
    write_yaml(path, raw)
    c = start(path)  # ADMIN 和 admin 算同一個帳號，不會因為「已存在」起不來
    h = login(c)
    users = {u["name"]: u for u in c.get("/web/api/users", headers=h).json()}
    assert sorted(users) == ["Kid", "admin"]
    r = c.delete(f"/web/api/users/{users['Kid']['id']}", headers=h)
    assert r.status_code == 200 and "設定檔" in r.json()["note"]
    assert [u.name for u in load_config(str(path)).users] == ["admin", "ADMIN"]
    assert c.get("/web/api/settings", headers=h).json()["problems"] == []

    c = start(path)
    h = login(c)
    assert [u["name"] for u in c.get("/web/api/users", headers=h).json()] == ["admin"]
    # 網頁上新增的帳號不在設定檔裡：刪掉時不用改檔案
    new = c.post("/web/api/users", json={"name": "guest", "password": "p"}, headers=h).json()
    assert c.delete(f"/web/api/users/{new['id']}", headers=h).status_code == 204


def test_timeouts_apply_without_restart(tmp_path: Path):
    path = tmp_path / "config.yaml"
    write_yaml(path, base(tmp_path))
    c = start(path)
    h = login(c)
    st = c.app.state
    assert st.p115._client.timeout.read == 15
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["p115"] = {"timeout": 42}
    raw["redirect"] = {"resolve_timeout": 7}
    write_yaml(path, raw)
    touch_later(path)
    c.get("/web/api/settings", headers=h)
    assert st.p115._client.timeout.read == 42 and st.p115.open._client.timeout.read == 42
    assert st.redirector.config.resolve_timeout == 7  # 每次解析重導向時照目前的設定


def test_update_proxy_settings_are_checked(tmp_path: Path):
    path = tmp_path / "config.yaml"
    write_yaml(path, base(tmp_path))
    c = start(path)
    h = login(c)
    for bad, why in (({"update_proxy": "127.0.0.1:7890"}, "socks5://"), ({"update_github_proxy": "ghfast.top"}, "https://")):
        r = c.put("/web/api/settings", json={"server": bad}, headers=h)
        assert r.status_code == 400 and why in r.text
    good = {"update_proxy": " socks5://127.0.0.1:1080 ", "update_github_proxy": "https://ghfast.top/"}
    assert c.put("/web/api/settings", json={"server": good}, headers=h).status_code == 200
    saved = load_config(str(path)).server
    assert (saved.update_proxy, saved.update_github_proxy) == ("socks5://127.0.0.1:1080", "https://ghfast.top/")
    # 轉不成數字的回 400（以前 1e999 是 500）；負的間隔當成 0（負的 request_delay 會讓逐層列目錄時 time.sleep 出錯）
    assert c.put("/web/api/settings", json={"p115": {"strm": {"interval": "1e999"}}}, headers=h).status_code == 400
    assert c.put("/web/api/settings", json={"p115": {"strm": {"request_delay": -1}}}, headers=h).status_code == 200
    assert load_config(str(path)).p115.strm.request_delay == 0
