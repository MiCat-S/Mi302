"""拼音排序、拼音搜尋、繁簡互通。"""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.db import Database
from embyserver.textutil import pinyin_full, pinyin_initials, search_text, sort_key


def test_sort_key_and_search_text():
    assert sort_key("流浪地球2") == "liu lang di qiu 2"
    assert sort_key("爱·回家之开心速递") == "ai hui jia zhi kai xin su di"
    assert sort_key("长安十二时辰").startswith("chang an")  # 多音字
    assert sort_key("Rock'n Roll") == "rock'n roll"  # 非中文照舊
    assert (pinyin_full("慶餘年"), pinyin_initials("庆余年")) == ("qingyunian", "qyn")
    assert search_text("慶餘年", "Joy of Life") == "庆馀年|joy of life|qingyunian|qyn"
    assert search_text("Dark", None) == "dark"


def make(tmp_path: Path):
    for name in ("庆余年 (2019)", "长安十二时辰 (2019)", "Dark (2017)", "爱情公寓 (2009)"):
        ep = tmp_path / "tv" / name / "S01E01.strm"
        ep.parent.mkdir(parents=True)
        ep.write_text("http://x/a.mkv")
    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")},
        "users": [{"name": "admin", "password": "pw", "admin": True}],
        "libraries": [{"name": "劇集", "type": "tvshows", "paths": [str(tmp_path / "tv")]}],
    }), scan_on_start=False)
    app.state.scanner.scan_all()
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    return app, c, h


def names(c, h, **params):
    q = {"IncludeItemTypes": "Series", "Recursive": "true", **params}
    return [i["Name"] for i in c.get("/Items", params=q, headers=h).json()["Items"]]


def test_sorting_and_searching_by_pinyin(tmp_path: Path):
    app, c, h = make(tmp_path)
    # 拼音排序：ai < chang < dark < qing
    assert names(c, h, SortBy="SortName") == ["爱情公寓", "长安十二时辰", "Dark", "庆余年"]
    assert names(c, h, NameStartsWith="Q") == ["庆余年"]  # 按字母跳轉
    for term in ("qyn", "qingyu", "庆余", "慶餘年", "QYN"):
        assert names(c, h, SearchTerm=term) == ["庆余年"], term
    assert names(c, h, SearchTerm="dark") == ["Dark"]


def test_old_database_gets_new_column(tmp_path: Path):
    path = tmp_path / "old.db"
    Database(path).conn.close()
    conn = sqlite3.connect(path)  # 模擬舊版：還沒有 search_text 欄位
    conn.execute("INSERT INTO items(name, path, type) VALUES('庆余年', '/tv/x', 'Series')")
    conn.execute("ALTER TABLE items DROP COLUMN search_text")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.one("SELECT name FROM items")["name"] == "庆余年"  # 舊資料還在
    assert "search_text" in {r[1] for r in db.conn.execute("PRAGMA table_info(items)")}


def test_fill_series_search_understands_pinyin(tmp_path: Path):
    from embyserver.moviepilot import library_series

    app, c, h = make(tmp_path)
    assert [s["name"] for s in library_series(app.state.db, "qyn")[0]] == ["庆余年"]
    assert [s["name"] for s in library_series(app.state.db, "慶餘年")[0]] == ["庆余年"]
