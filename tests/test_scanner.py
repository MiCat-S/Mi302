"""掃描器：劇集媒體庫裡的分類資料夾、中文季數與集數。"""

from pathlib import Path

from embyserver.config import config_from_dict
from embyserver.db import Database
from embyserver.scanner import Scanner, looks_like_series, parse_episode, parse_season_dir


def touch(path: Path, text: str = "http://cdn.example.com/x.mkv") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def scan(tmp_path: Path, paths) -> Database:
    db = Database(":memory:")
    config = config_from_dict({"libraries": [{"name": "劇集", "type": "tvshows", "paths": [str(p) for p in paths]}]})
    Scanner(db, config).scan_all()
    return db


def names(db: Database, kind: str) -> list:
    return sorted(r["name"] for r in db.query("SELECT name FROM items WHERE type=?", (kind,)))


def test_category_folders_are_not_series(tmp_path: Path):
    tv = tmp_path / "电视剧"
    touch(tv / "国产剧" / "庆余年 (2019)" / "Season 1" / "庆余年.S01E01.strm")
    touch(tv / "国产剧" / "庆余年 (2019)" / "Season 1" / "庆余年.S01E02.strm")
    touch(tv / "日番" / "葬送的芙莉莲" / "第一季" / "第01集.strm")
    touch(tv / "欧美剧" / "Dark" / "tvshow.nfo", "<tvshow><title>暗黑</title></tvshow>")
    touch(tv / "欧美剧" / "Dark" / "Dark.S01E01.strm")
    touch(tv / "综艺" / "奔跑吧" / "奔跑吧 第十二集.strm")
    touch(tv / "儿童" / "空分類" / "readme.txt")
    touch(tv / "@eaDir" / "x" / "x.S01E01.strm")

    db = scan(tmp_path, [tv])
    assert names(db, "Series") == ["奔跑吧", "庆余年", "暗黑", "葬送的芙莉莲"]
    eps = db.query("SELECT name, index_number, parent_index_number FROM items WHERE type='Episode' ORDER BY path")
    assert len(eps) == 5
    season = db.one("SELECT i.index_number FROM items i JOIN items s ON s.id = i.series_id WHERE s.name='葬送的芙莉莲' AND i.type='Episode'")
    assert season["index_number"] == 1
    rp = db.one("SELECT index_number FROM items WHERE type='Episode' AND path LIKE '%奔跑吧 第十二集%'")
    assert rp["index_number"] == 12

    # 每個分類各加一次也一樣
    db2 = scan(tmp_path, [tv / "国产剧", tv / "日番", tv / "欧美剧", tv / "综艺", tv / "儿童"])
    assert names(db2, "Series") == names(db, "Series")


def test_series_detection():
    assert parse_season_dir("第二季") == 2 and parse_season_dir("第十一季") == 11 and parse_season_dir("Season 3") == 3
    assert parse_episode("某剧 第一百零五集") == (None, 105)
    assert parse_episode("Show.S02E07.1080p") == (2, 7)
    # 開頭就是集號（綜藝常見）
    assert parse_episode("10.潘玮柏战队面临团危机-蓝光4K") == (None, 10)
    assert parse_episode("03-比赛惊现死亡之组-蓝光4K") == (None, 3)
    assert parse_episode("9.吴亦凡热狗助攻队员开启6强PK-蓝光1080P") == (None, 9)
    assert parse_episode("01 嘻哈首战") == (None, 1) and parse_episode("07") == (None, 7)
    # 不是集號的數字
    assert parse_episode("21 Jump Street (2012)") == (None, None)
    assert parse_episode("2019.12.01 某节目") == (None, None)
    assert parse_episode("1080p") == (None, None)


def test_episode_nfo_with_unknown_numbers(tmp_path: Path):
    """刮削時沒認出集號，nfo 寫 -1：改從季資料夾和檔名判斷，不能整季都變成第 -1 集。"""
    season = tmp_path / "tv" / "中国新说唱 (2017)" / "Season 01"
    for stem in ("10.潘玮柏战队面临团危机-蓝光4K", "03-比赛惊现死亡之组-蓝光4K", "01-嘻哈首战-蓝光4K", "无法识别的特辑"):
        touch(season / f"{stem}.strm")
        touch(season / f"{stem}.nfo", f"<episodedetails><title>{stem}</title><season>-1</season><episode>-1</episode></episodedetails>")
    db = scan(tmp_path, [tmp_path / "tv"])
    rows = db.query("SELECT parent_index_number AS s, index_number AS e FROM items WHERE type='Episode' ORDER BY e")
    assert [(r["s"], r["e"]) for r in rows] == [(1, None), (1, 1), (1, 3), (1, 10)]


def test_looks_like_series(tmp_path: Path):
    touch(tmp_path / "国产剧" / "某剧" / "S01" / "e1.strm")
    assert not looks_like_series(tmp_path / "国产剧")
    assert looks_like_series(tmp_path / "国产剧" / "某剧")
    (tmp_path / "新剧 (2024)").mkdir()
    assert looks_like_series(tmp_path / "新剧 (2024)")


def test_unreadable_files_do_not_stop_the_scan(tmp_path: Path):
    """壞掉的符號連結、沒權限的資料夾只略過那一項，其他的照掃。"""
    import os

    from embyserver.people import PeopleStore

    movies = tmp_path / "电影"
    touch(movies / "Good (2020)" / "Good (2020).strm")
    (movies / "Broken (2021)").mkdir()
    os.symlink(tmp_path / "nowhere.mkv", movies / "Broken (2021)" / "Broken (2021).mkv")  # 指向不存在的檔案
    tv = tmp_path / "电视剧"
    touch(tv / "Dark" / "Dark.S01E01.strm")
    (tv / "Dark" / "Dark.S01E02.mkv").symlink_to(tmp_path / "gone.mkv")
    db = Database(":memory:")
    config = config_from_dict({"libraries": [
        {"name": "電影", "type": "movies", "paths": [str(movies)]},
        {"name": "劇集", "type": "tvshows", "paths": [str(tv)]},
    ]})
    Scanner(db, config).scan_all()
    assert names(db, "Movie") == ["Good"] and names(db, "Episode") == ["第 1 集"]

    # nfo 只寫年份：不能變成 2019T00:00:00 這種假日期；拿掉演員時資料庫跟著清
    show = tv / "Dark"
    touch(show / "tvshow.nfo", "<tvshow><title>Dark</title><premiered>2019</premiered><actor><name>A</name></actor></tvshow>")
    scanner = Scanner(db, config)
    scanner.scan_all()
    row = db.one("SELECT * FROM items WHERE type='Series'")
    assert (row["year"], row["premiere_date"]) == (2019, None)
    assert [p["Name"] for p in PeopleStore(db).for_item(row)] == ["A"]
    touch(show / "tvshow.nfo", "<tvshow><title>Dark</title><premiered>2019-06-27</premiered></tvshow>")
    scanner.scan_all()
    row = db.one("SELECT * FROM items WHERE type='Series'")
    assert row["premiere_date"] == "2019-06-27T00:00:00.0000000Z"
    assert PeopleStore(db).for_item(row) == []


def test_stopped_scan_keeps_what_it_did_not_reach(tmp_path: Path):
    """程式結束時掃描停在兩項之間：還沒掃到的不能當成已經刪掉（連同觀看紀錄）。"""
    tv = tmp_path / "tv"
    for name in ("Alpha", "Beta", "Gamma"):
        touch(tv / name / f"{name}.S01E01.strm")
    db = Database(":memory:")
    scanner = Scanner(db, config_from_dict({"libraries": [{"name": "劇集", "type": "tvshows", "paths": [str(tv)]}]}))
    scanner.scan_all()
    real = scanner._add_series

    def add_then_stop(*a):
        real(*a)
        scanner.stop()

    scanner._add_series = add_then_stop
    scanner.scan_all()
    assert names(db, "Series") == ["Alpha", "Beta", "Gamma"] and len(names(db, "Episode")) == 3 and not scanner.scanning
