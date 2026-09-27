"""挑選要探測媒體資訊的影片：篩選、先做哪些、這次最多幾支、只做某幾部。"""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.mediainfo import MediaInfoStore


def touch(path: Path, text: str = "http://cdn.example.com/x.mkv") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def build(tmp_path: Path, enabled: bool = True):
    movies, tv = tmp_path / "movies", tmp_path / "tv"
    for name, year, rating in (("Old (2010)", 2010, 6.0), ("Mid (2020)", 2020, 8.5), ("New (2023)", 2023, 7.0)):
        touch(movies / name / f"{name}.strm")
        touch(movies / name / "movie.nfo", f"<movie><title>{name[:-7]}</title><year>{year}</year><rating>{rating}</rating></movie>")
    qyn = tv / "国产剧" / "庆余年 (2019)"
    touch(qyn / "tvshow.nfo", "<tvshow><title>庆余年</title><year>2019</year><rating>8.0</rating></tvshow>")
    for e in (1, 2, 3):
        touch(qyn / "Season 1" / f"庆余年.S01E0{e}.strm")
    dark = tv / "Dark (2017)"
    touch(dark / "tvshow.nfo", "<tvshow><title>Dark</title><year>2017</year><rating>9.0</rating></tvshow>")
    for e in (1, 2):
        touch(dark / "Season 1" / f"Dark.S01E0{e}.strm")
    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")},
        "users": [{"name": "admin", "password": "pw", "admin": True}],
        "libraries": [
            {"name": "電影", "type": "movies", "paths": [str(movies)]},
            {"name": "劇集", "type": "tvshows", "paths": [str(tv)]},
        ],
        "mediainfo": {"enabled": enabled, "ffprobe": sys.executable},
    }), scan_on_start=False)
    app.state.scanner.scan_all()
    # 庆余年第 1 集已經有媒體資訊
    MediaInfoStore(app.state.db).put(str(qyn / "Season 1" / "庆余年.S01E01.strm"), {"source": {}}, 0, "ffprobe")
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    calls = []
    app.state.prober.run_in_background = lambda paths, source, label="", limit=0, spec=None: calls.append((paths, label)) or True
    return app, c, h, calls


def titles(c, h, **params):
    return c.get("/web/api/mediainfo/titles", params=params, headers=h).json()


def names(r):
    return [i["name"] for i in r["items"]]


def test_titles_filters_and_order(tmp_path: Path):
    app, c, h, _ = build(tmp_path)
    r = titles(c, h)
    assert (r["total"], r["movies"], r["episodes"]) == (5, 3, 4)  # 庆余年第 1 集有了，不算
    qyn = next(i for i in r["items"] if i["name"] == "庆余年")
    assert (qyn["type"], qyn["missing"], qyn["total"], qyn["year"]) == ("Series", 2, 3, 2019)
    assert [lib["name"] for lib in r["libraries"]] == ["電影", "劇集"]

    assert names(titles(c, h, order="year_desc")) == ["New", "Mid", "庆余年", "Dark", "Old"]
    assert names(titles(c, h, order="year_asc"))[:2] == ["Old", "Dark"]
    assert names(titles(c, h, order="rating")) == ["Dark", "Mid", "庆余年", "New", "Old"]
    # 劇集、2018 年以後：只剩庆余年；年份填反了也行
    assert names(titles(c, h, kind="series", year_from=2018)) == ["庆余年"]
    assert sorted(names(titles(c, h, year_from=2021, year_to=2019))) == ["Mid", "庆余年"]  # 預設順序看加入時間，這裡只驗篩選
    # 首字母、全拼、繁體都找得到
    for q in ("qyn", "qingyunian", "慶餘年"):
        assert names(titles(c, h, q=q)) == ["庆余年"], q
    tv = next(lib["id"] for lib in r["libraries"] if lib["name"] == "劇集")
    assert sorted(names(titles(c, h, library=tv))) == ["Dark", "庆余年"]
    assert titles(c, h, limit=2, offset=4)["items"][0]["name"] in names(r)  # 分頁


def test_probe_picks_in_order_up_to_the_limit(tmp_path: Path):
    app, c, h, calls = build(tmp_path)
    r = c.post("/web/api/mediainfo/probe", json={"order": "year_asc", "limit": 3}, headers=h).json()
    assert (r["started"], r["count"]) == (True, 3)
    paths, label = calls[-1]
    assert [Path(p).name for p in paths] == ["Old (2010).strm", "Dark.S01E01.strm", "Dark.S01E02.strm"]
    assert label == "年份舊的先做・最多 3 支"

    # 只做某一部劇：照季、集順序，已經有的第 1 集不做
    qyn = next(i for i in titles(c, h)["items"] if i["name"] == "庆余年")["id"]
    r = c.post("/web/api/mediainfo/probe", json={"ids": [qyn]}, headers=h).json()
    assert [Path(p).name for p in calls[-1][0]] == ["庆余年.S01E02.strm", "庆余年.S01E03.strm"]
    assert calls[-1][1] == "「庆余年」"

    # 沒有符合的：不開始
    r = c.post("/web/api/mediainfo/probe", json={"year_from": 2030}, headers=h).json()
    assert (r["started"], r["count"], r["busy"]) == (False, 0, False)

    # 正在探測中：不開始，告訴網頁
    app.state.prober._lock.acquire()
    try:
        r = c.post("/web/api/mediainfo/probe", json={}, headers=h).json()
        assert (r["started"], r["busy"]) == (False, True)
    finally:
        app.state.prober._lock.release()

    assert c.post("/web/api/mediainfo/probe", json={"limit": "很多"}, headers=h).status_code == 400
    assert c.post("/web/api/mediainfo/probe", json=[1], headers=h).status_code == 400


def test_probe_needs_batch_switch(tmp_path: Path):
    app, c, h, calls = build(tmp_path, enabled=False)
    assert titles(c, h)["total"] == 5  # 清單照樣看得到
    r = c.post("/web/api/mediainfo/probe", json={"limit": 5}, headers=h)
    assert r.status_code == 400 and "批次探測" in r.text and not calls
