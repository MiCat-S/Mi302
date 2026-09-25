"""部分掃描：只掃一部劇、一個分類、一部片或一個媒體庫；同步和 MoviePilot 通知後只掃有變動的地方。"""

import shutil
from pathlib import Path

from embyserver.config import config_from_dict
from embyserver.db import Database
from embyserver.scanner import Scanner


def touch(path: Path, text: str = "http://cdn.example.com/x.mkv") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def setup(tmp_path: Path):
    tv, movies = tmp_path / "电视剧", tmp_path / "电影"
    touch(tv / "国产剧" / "庆余年 (2019)" / "Season 1" / "庆余年.S01E01.strm")
    touch(tv / "日番" / "芙莉莲 (2023)" / "Season 1" / "芙莉莲.S01E01.strm")
    touch(movies / "华语电影" / "英雄 (2002)" / "英雄 (2002).strm")
    touch(movies / "Loose.Movie.2021.strm")
    config = config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")},
        "libraries": [
            {"name": "劇集", "type": "tvshows", "paths": [str(tv)]},
            {"name": "電影", "type": "movies", "paths": [str(movies)]},
        ],
    })
    db = Database(":memory:")
    scanner = Scanner(db, config)
    scanner.scan_all()
    return tv, movies, config, db, scanner


def ids(db: Database, kind: str) -> dict:
    return {r["name"]: r["id"] for r in db.query("SELECT id, name FROM items WHERE type=?", (kind,))}


def episodes(db: Database) -> list:
    return sorted(Path(r["path"]).name for r in db.query("SELECT path FROM items WHERE type='Episode'"))


def test_scan_one_series_only_touches_that_series(tmp_path: Path):
    tv, movies, config, db, scanner = setup(tmp_path)
    before = ids(db, "Series")
    touch(tv / "国产剧" / "庆余年 (2019)" / "Season 1" / "庆余年.S01E02.strm")
    touch(tv / "日番" / "芙莉莲 (2023)" / "Season 1" / "芙莉莲.S01E02.strm")  # 不在範圍內，不該被掃到

    scanner.scan_paths([str(tv / "国产剧" / "庆余年 (2019)" / "Season 1" / "庆余年.S01E02.strm")])
    assert episodes(db) == ["庆余年.S01E01.strm", "庆余年.S01E02.strm", "芙莉莲.S01E01.strm"]
    assert ids(db, "Series") == before  # 劇集 id 不變，觀看紀錄不會掉
    assert scanner.touched == 4  # 劇、季、兩集


def test_new_and_deleted_series_in_category(tmp_path: Path):
    tv, movies, config, db, scanner = setup(tmp_path)
    touch(tv / "国产剧" / "繁花 (2023)" / "繁花.S01E01.strm")
    scanner.scan_paths([str(tv / "国产剧")])
    assert set(ids(db, "Series")) == {"庆余年", "芙莉莲", "繁花"}

    shutil.rmtree(tv / "国产剧" / "庆余年 (2019)")
    scanner.scan_paths([str(tv / "国产剧" / "庆余年 (2019)" / "Season 1" / "庆余年.S01E01.strm")])
    assert set(ids(db, "Series")) == {"芙莉莲", "繁花"}
    assert not db.query("SELECT id FROM items WHERE path LIKE '%庆余年%'")


def test_movie_folder_and_loose_file(tmp_path: Path):
    tv, movies, config, db, scanner = setup(tmp_path)
    touch(movies / "华语电影" / "英雄 (2002)" / "movie.nfo", "<movie><title>英雄</title><plot>俠客</plot></movie>")
    scanner.scan_paths([str(movies / "华语电影" / "英雄 (2002)" / "movie.nfo")])
    row = db.one("SELECT name, overview FROM items WHERE type='Movie' AND path LIKE '%英雄%'")
    assert row["overview"] == "俠客"

    (movies / "Loose.Movie.2021.strm").unlink()
    scanner.scan_paths([str(movies / "Loose.Movie.2021.strm")])
    assert set(ids(db, "Movie")) == {"英雄"}
    assert scanner.touched == 0  # 只移除那一部，沒有重掃整個媒體庫


def test_paths_outside_libraries_are_ignored(tmp_path: Path):
    tv, movies, config, db, scanner = setup(tmp_path)
    count = db.one("SELECT COUNT(*) AS c FROM items")["c"]
    scanner.scan_paths(["/somewhere/else/x.strm", str(tmp_path)])
    assert db.one("SELECT COUNT(*) AS c FROM items")["c"] == count
    assert not scanner.in_library(str(tmp_path)) and scanner.in_library(str(tv / "国产剧"))


def test_scan_one_library_and_drop_removed_library(tmp_path: Path):
    tv, movies, config, db, scanner = setup(tmp_path)
    touch(tv / "日番" / "新番 (2024)" / "新番.S01E01.strm")
    touch(movies / "新片 (2024)" / "新片 (2024).strm")
    scanner.scan_libraries(["劇集"])
    assert "新番" in ids(db, "Series") and "新片" not in ids(db, "Movie")

    config.libraries[:] = [lib for lib in config.libraries if lib.name == "劇集"]
    scanner.scan_libraries([])
    assert not ids(db, "Movie")
    assert [r["name"] for r in db.query("SELECT name FROM items WHERE type='CollectionFolder'")] == ["劇集"]
