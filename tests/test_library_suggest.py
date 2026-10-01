"""批量新增媒體庫：子資料夾的類型猜測、影片數、和現有媒體庫的關係。"""

from pathlib import Path

from embyserver import library_suggest
from embyserver.config import LibraryConfig
from fakes import admin_headers, make_client


def touch(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build(root: Path) -> None:
    for name in ("阿凡达 (2009)/阿凡达 (2009).strm", "Dune (2021)/Dune.mkv", "Heat/Heat.mp4"):
        touch(root / "华语片" / name)
    touch(root / "国产剧" / "庆余年 (2019)" / "tvshow.nfo")
    for ep in range(1, 5):
        touch(root / "国产剧" / "庆余年 (2019)" / "Season 1" / f"第{ep}集.strm")
    for ep in range(1, 4):
        touch(root / "Collection" / "Show" / "Season 1" / f"{ep:02d}.mkv")  # 只靠季資料夾認出劇集
    for ep in range(1, 4):
        touch(root / "Mixed" / f"Show.S01E0{ep}.mkv")
    (root / "动画电影").mkdir()
    touch(root / "综艺" / "某节目 20230101.mp4")
    (root / "纪录片").mkdir()
    touch(root / ".hidden" / "a.mkv")
    touch(root / "@eaDir" / "a.mkv")
    touch(root / "已有" / "a.mkv")


def test_suggest_guesses_types_and_defaults(tmp_path: Path):
    build(tmp_path)
    libs = [LibraryConfig(name="舊的", type="movies", paths=[str(tmp_path / "已有")])]
    r = library_suggest.suggest(str(tmp_path), libs)
    got = {f["name"]: f for f in r["folders"]}
    assert set(got) == {"华语片", "国产剧", "Collection", "Mixed", "动画电影", "综艺", "纪录片", "已有"}
    assert [f["name"] for f in r["folders"]] == sorted(got, key=str.lower)
    assert got["华语片"]["type"] == "movies" and got["华语片"]["videos"] == 3
    assert got["国产剧"]["type"] == "tvshows" and got["国产剧"]["videos"] == 4
    assert got["Collection"]["type"] == "tvshows"
    assert got["Mixed"]["type"] == "tvshows"
    assert got["动画电影"]["type"] == "movies" and got["动画电影"]["videos"] == 0
    assert got["综艺"]["type"] == "tvshows" and "名稱" in got["综艺"]["why"]  # 影片太少，看名稱
    assert got["纪录片"]["type"] == "movies"
    # 空資料夾、已經在媒體庫裡的預設不勾
    assert got["华语片"]["checked"] and got["综艺"]["checked"]
    assert not got["动画电影"]["checked"] and not got["纪录片"]["checked"]
    assert not got["已有"]["checked"] and "舊的" in got["已有"]["used"]
    assert not r["partial"] and got["华语片"]["complete"]


def test_used_by_covers_parent_child_and_same(tmp_path: Path):
    libs = [LibraryConfig(name="劇集", type="tvshows", paths=[str(tmp_path / "tv"), str(tmp_path / "x" / "deep") + "/"])]
    assert "已經是" in library_suggest.used_by(str(tmp_path / "tv"), libs)
    assert "包含在" in library_suggest.used_by(str(tmp_path / "tv" / "国产剧"), libs)
    assert "裡面有" in library_suggest.used_by(str(tmp_path / "x"), libs)
    assert library_suggest.used_by(str(tmp_path / "tv2"), libs) is None  # 名稱開頭相同不算


def test_type_by_name():
    assert library_suggest.type_by_name("动画电影") == "movies"
    assert library_suggest.type_by_name("剧场版") == "movies"
    assert library_suggest.type_by_name("日番") == "tvshows"
    assert library_suggest.type_by_name("TV Shows") == "tvshows"
    assert library_suggest.type_by_name("Movies") == "movies"
    assert library_suggest.type_by_name("纪录片") is None


def test_out_of_time_falls_back_to_names(tmp_path: Path, monkeypatch):
    build(tmp_path)
    monkeypatch.setattr(library_suggest, "TOTAL_SECONDS", 0)
    r = library_suggest.suggest(str(tmp_path), [])
    got = {f["name"]: f for f in r["folders"]}
    assert r["partial"] and got["国产剧"]["videos"] is None and not got["国产剧"]["checked"]
    assert got["国产剧"]["type"] == "tvshows" and got["华语片"]["type"] == "movies"


def test_suggest_endpoint(tmp_path: Path):
    build(tmp_path / "media")
    c = make_client(tmp_path, {"users": [{"name": "admin", "password": "pw", "admin": True}]})
    h = admin_headers(c)
    r = c.get("/web/api/libraries/suggest", params={"path": str(tmp_path / "media")}, headers=h)
    assert r.status_code == 200 and len(r.json()["folders"]) == 8
    assert c.get("/web/api/libraries/suggest", params={"path": str(tmp_path / "nope")}, headers=h).status_code == 400
    assert c.get("/web/api/libraries/suggest", params={"path": str(tmp_path)}).status_code == 401
