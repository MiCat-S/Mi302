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


def test_looks_like_series(tmp_path: Path):
    touch(tmp_path / "国产剧" / "某剧" / "S01" / "e1.strm")
    assert not looks_like_series(tmp_path / "国产剧")
    assert looks_like_series(tmp_path / "国产剧" / "某剧")
    (tmp_path / "新剧 (2024)").mkdir()
    assert looks_like_series(tmp_path / "新剧 (2024)")
