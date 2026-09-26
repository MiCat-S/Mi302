"""全量同步用 115 導出目錄樹：解析目錄樹、用檔名對出資料夾路徑、失敗時改回逐層列目錄。"""

from pathlib import Path

import httpx
import pytest

from embyserver.p115 import P115Error, parse_export_tree, tree_relative
from embyserver.strm_sync import FULL, INCREMENTAL, _TaskIndex, _task_key, match_tree_dirs

from test_incremental import T0, Fake115, make


def listings(fake: Fake115):
    """逐層列目錄的請求（cur=1），不含只查路徑的 limit=1。"""
    return [c for c in fake.calls if c[0] == "/files" and c[1].get("cur") == "1" and c[1].get("limit") != "1"]


def lookups(fake: Fake115):
    """查一個資料夾路徑的請求。"""
    return [int(c[1]["cid"]) for c in fake.calls if c[0] == "/files" and c[1].get("limit") == "1"]


def test_parse_export_tree():
    text = (
        "|——根目录\r\n| |-影視\r\n| | |-電影\r\n| | | |-Rock\\'n Roll (1999)\r\n"
        "| | | | |-Rock\\'n Roll (1999).mkv\r\n| | |-劇集\r\n| | | |-第一行\r\n第二行.mkv\r\n"
    )
    nodes = parse_export_tree(text.encode("utf-16"))
    assert nodes == [
        ("影視",), ("影視", "電影"), ("影視", "電影", "Rock'n Roll (1999)"),
        ("影視", "電影", "Rock'n Roll (1999)", "Rock'n Roll (1999).mkv"),
        ("影視", "劇集"), ("影視", "劇集", "第一行\n第二行.mkv"),
    ]
    # 沒有 BOM 的 UTF-16LE、UTF-8 也看得懂
    assert parse_export_tree(text.encode("utf-16-le")) == nodes
    assert parse_export_tree(text.encode("utf-8")) == nodes

    assert tree_relative(nodes, "/影視")[:2] == [("電影",), ("電影", "Rock'n Roll (1999)")]
    # 目錄樹帶著完整路徑時也可以
    deep = [("A",), ("A", "影視"), ("A", "影視", "x.mkv")]
    assert tree_relative(deep, "/A/影視") == [("x.mkv",)]
    with pytest.raises(P115Error):
        tree_relative([("別的",), ("另一個",)], "/影視")


def test_match_dirs_with_generic_names():
    tree = [
        ("番", "芙莉莲"), ("番", "芙莉莲", "Season 1"), ("番", "芙莉莲", "Season 1", "01.mkv"),
        ("番", "芙莉莲", "Season 1", "02.mkv"), ("番", "芙莉莲", "tvshow.nfo"),
        ("番", "葬送"), ("番", "葬送", "Season 1"), ("番", "葬送", "Season 1", "01.mkv"),
        ("番", "葬送", "Season 1", "02.mkv"), ("番", "葬送", "Season 1", "葬送.S01E03.mkv"),
        ("番", "葬送", "tvshow.nfo"),
        ("雙胞胎A", "01.mkv"), ("雙胞胎B", "01.mkv"),
    ]
    files = [
        {"parent_id": 11, "name": "01.mkv"}, {"parent_id": 11, "name": "02.mkv"},
        {"parent_id": 21, "name": "01.mkv"}, {"parent_id": 21, "name": "葬送.S01E03.mkv"},
        {"parent_id": 10, "name": "tvshow.nfo"}, {"parent_id": 20, "name": "tvshow.nfo"},
        {"parent_id": 30, "name": "01.mkv"}, {"parent_id": 31, "name": "01.mkv"},
        {"parent_id": 40, "name": "導出之後才上傳.mkv"},
        {"parent_id": 1, "name": "直接放在任務目錄.mkv"},
    ]
    dirs, unmatched = match_tree_dirs(tree, files, 1, hints={20: "番/葬送"})
    assert dirs[21] == "番/葬送/Season 1"  # 有一集的檔名是獨特的
    assert dirs[11] == "番/芙莉莲/Season 1"  # 另一個 Season 1 已經被確定，剩下這個
    assert dirs[20] == "番/葬送" and dirs[10] == "番/芙莉莲"  # 上次同步記下的 id，再排除
    assert dirs[1] == ""
    # 內容完全一樣的兩個資料夾、目錄樹裡沒有的檔案：要另外查
    assert sorted(unmatched) == [30, 31, 40]


def test_match_dirs_never_gives_one_path_to_two_folders():
    tree = [("A", "S1", "01.mkv"), ("B", "S1", "01.mkv"), ("X", "獨特.mkv")]
    files = [
        {"parent_id": 11, "name": "01.mkv"}, {"parent_id": 21, "name": "01.mkv"},
        {"parent_id": 31, "name": "01.mkv"},  # 導出之後才建的資料夾，檔名剛好一樣
        {"parent_id": 41, "name": "獨特.mkv"}, {"parent_id": 42, "name": "獨特.mkv"},  # 導出之後複製了一份
    ]
    dirs, unmatched = match_tree_dirs(tree, files, 1, hints={11: "A/S1"})
    assert dirs == {1: "", 11: "A/S1"}
    assert sorted(unmatched) == [21, 31, 41, 42]


def test_full_sync_uses_export_tree(tmp_path: Path):
    fake = Fake115()
    fake.dirs.update({104: ("Season 1", 103), 105: ("Season 2", 103)})
    fake.files += [
        {"fid": 3, "cid": 104, "n": "01.mkv", "pc": "c" * 17, "s": 900_000_000, "te": T0 + 20},
        {"fid": 4, "cid": 105, "n": "01.mkv", "pc": "d" * 17, "s": 900_000_000, "te": T0 + 30},
    ]
    sync = make(tmp_path, fake)
    r = sync.run(FULL)
    media = tmp_path / "media"
    assert not r.errors and not r.notes
    assert (media / "電影" / "Old Movie (2001).strm").read_text().endswith(f"/d/{'a' * 17}.mkv")
    assert (media / "劇集" / "Dark" / "Dark.S01E01.strm").exists()
    # 兩季都只有 01.mkv，分不出來：各查一次路徑，不必逐層列目錄
    assert (media / "劇集" / "Dark" / "Season 1" / "01.strm").read_text().endswith(f"/d/{'c' * 17}.mkv")
    assert (media / "劇集" / "Dark" / "Season 2" / "01.strm").read_text().endswith(f"/d/{'d' * 17}.mkv")
    assert listings(fake) == []
    assert len([c for c in fake.calls if c[0] == "/files" and c[1].get("limit") == "1"]) == 2
    assert fake.deleted == ["9500"]  # 用完把 115 根目錄的目錄樹檔案刪掉
    index = _TaskIndex(sync.p115.db, _task_key(sync.tasks[0]))
    assert index.dirs()[104] == "劇集/Dark/Season 1"
    assert index.dirs()[102] == "劇集"  # 只有子資料夾的資料夾，查 Season 1 時順便拿到

    # 第二次全量：上次記下了資料夾 id，連路徑都不用查
    fake.calls.clear()
    r = sync.run(FULL)
    assert r.strm_created == 0 and r.strm_unchanged == 4
    assert [c for c in fake.calls if c[0] == "/files" and c[1].get("cur") != "0"] == []


def test_full_sync_falls_back_to_walk(tmp_path: Path):
    fake = Fake115()
    fake.export_ok = False  # 例如 115 正在跑別的導出任務
    sync = make(tmp_path, fake)
    r = sync.run(FULL)
    assert r.strm_created == 2 and not r.errors
    assert any("改成逐層列目錄" in n for n in r.notes)
    assert listings(fake)


def test_incomplete_listing_falls_back_and_never_deletes(tmp_path: Path):
    fake = Fake115()
    sync = make(tmp_path, fake, delete_stale=True)
    sync.run(FULL)
    media = tmp_path / "media"

    # 115 的遞迴列檔少給了東西：不能照它刪，改回逐層列目錄
    real = fake.handler

    def short(request):
        resp = real(request)
        if request.url.path == "/files" and request.url.params.get("cur") == "0":
            data = resp.json()
            data["data"] = data["data"][:1]
            data["count"] = 1
            return type(resp)(200, json=data)
        return resp

    sync.p115._client._transport = type(sync.p115._client._transport)(short)
    r = sync.run(FULL)
    assert any("比目錄樹少" in n for n in r.notes)
    assert (media / "電影" / "Old Movie (2001).strm").exists()
    assert (media / "劇集" / "Dark" / "Dark.S01E01.strm").exists()
    assert r.removed == 0


def test_folders_with_only_subfolders_are_indexed(tmp_path: Path):
    fake = Fake115()
    fake.dirs.update({
        110: ("電視劇", 100), 111: ("國產劇", 110), 112: ("繁花", 111), 113: ("Season 1", 112),
        114: ("漫長的季節", 111), 115: ("Season 1", 114),
    })
    fake.files += [
        {"fid": 5, "cid": 113, "n": "繁花.S01E01.mkv", "pc": "e" * 17, "s": 900_000_000, "te": T0 + 40},
        {"fid": 6, "cid": 115, "n": "漫長的季節.S01E01.mkv", "pc": "f" * 17, "s": 900_000_000, "te": T0 + 50},
    ]
    sync = make(tmp_path, fake, delete_stale=True)
    r = sync.run(FULL)
    assert not r.errors and not r.notes
    # 劇集資料夾只放各季：每部劇查一次（從 Season 1 往上），分類資料夾順便補上
    assert sorted(lookups(fake)) == [103, 113, 115]
    dirs = _TaskIndex(sync.p115.db, _task_key(sync.tasks[0])).dirs()
    assert {d: dirs[d] for d in (102, 110, 111, 112, 114)} == {
        102: "劇集", 110: "電視劇", 111: "電視劇/國產劇", 112: "電視劇/國產劇/繁花", 114: "電視劇/國產劇/漫長的季節",
    }
    fake.calls.clear()
    sync.run(FULL)
    assert lookups(fake) == []  # 第二次用上次記下的 id

    # 增量：整部劇改名時，本機連 MoviePilot 刮的 tvshow.nfo 一起搬
    fake.event(2, 5)
    assert not sync.run(INCREMENTAL).errors
    show = tmp_path / "media" / "電視劇" / "國產劇" / "繁花"
    (show / "tvshow.nfo").write_text("<tvshow/>")
    fake.dirs[112] = ("繁花 (2023)", 111)
    fake.event(20, 112, is_dir=True)
    fake.calls.clear()
    r = sync.run(INCREMENTAL)
    assert not r.errors and not r.fell_back_to_full and r.moved == 1
    moved = show.parent / "繁花 (2023)"
    assert (moved / "tvshow.nfo").read_text() == "<tvshow/>"
    assert (moved / "Season 1" / "繁花.S01E01.strm").exists()
    assert not show.exists()


def test_lookup_corrects_a_wrong_match(tmp_path: Path):
    fake = Fake115()
    fake.dirs[104] = ("Season 1", 103)
    sync = make(tmp_path, fake)
    task = sync.tasks[0]
    # 999 被錯對到 Season 1；115 說那是 104，999 已不存在
    dirs = {100: "", 999: "劇集/Dark/Season 1"}
    sync._resolve_tree_dirs(task, dirs, [104], {"劇集", "劇集/Dark", "劇集/Dark/Season 1"}, {})
    assert dirs == {100: "", 102: "劇集", 103: "劇集/Dark", 104: "劇集/Dark/Season 1"}


def test_export_status_error_stops_polling(tmp_path: Path):
    fake = Fake115()
    sync = make(tmp_path, fake)
    polls = []
    real = fake.handler

    def failing(request):
        if request.url.path == "/files/export_dir" and request.method == "GET":
            polls.append(1)
            return httpx.Response(200, json={"state": False, "errNo": 990001, "error": "导出任务不存在", "data": []})
        return real(request)

    sync.p115._client._transport = httpx.MockTransport(failing)
    # 任務失敗或被取消：立刻放棄，不會輪詢到超時
    with pytest.raises(P115Error, match="導出目錄樹失敗"):
        sync.p115.export_tree(100, "/影視", timeout=60)
    assert len(polls) == 1
    r = sync.run(FULL)
    assert r.strm_created == 2 and not r.errors
    assert any("改成逐層列目錄" in n for n in r.notes)
    assert listings(fake)


def test_videos_missing_from_listing_are_kept(tmp_path: Path):
    fake = Fake115()
    for i in range(18):
        fake.files.append(
            {"fid": 10 + i, "cid": 101, "n": f"Movie {i:02d} (2000).mkv", "pc": f"{i:017d}", "s": 900_000_000, "te": T0 + i}
        )
    sync = make(tmp_path, fake, delete_stale=True)
    assert sync.run(FULL).strm_created == 20
    media = tmp_path / "media"
    dark = media / "劇集" / "Dark" / "Dark.S01E01.strm"
    (dark.parent / "Dark.S01E01.nfo").write_text("<episodedetails/>")
    # 115 真的刪了 Old Movie；Dark.S01E01 還在目錄樹裡，只是遞迴列檔少給了（少 1/19，沒到改回逐層的門檻）
    fake.files = [f for f in fake.files if f["fid"] != 1]
    real = fake.handler

    def short(request):
        resp = real(request)
        if request.url.path == "/files" and request.url.params.get("cur") == "0":
            data = resp.json()
            data["data"] = [f for f in data["data"] if f["fid"] != 2]
            data["count"] = len(data["data"])
            return httpx.Response(200, json=data)
        return resp

    sync.p115._client._transport = httpx.MockTransport(short)
    r = sync.run(FULL)
    assert not r.errors and any("保留" in n for n in r.notes)
    # 兩邊都沒有的才刪；只是沒列出來的連刮削資料一起留著
    assert not (media / "電影" / "Old Movie (2001).strm").exists() and r.removed == 1
    assert dark.exists() and (dark.parent / "Dark.S01E01.nfo").exists()
    index = _TaskIndex(sync.p115.db, _task_key(sync.tasks[0]))
    assert index.get(2) == ("劇集/Dark/Dark.S01E01.strm", False)  # 索引也留著，增量遇到它的事件才找得到
    assert index.get(1) is None


def test_listing_error_falls_back_to_walk(tmp_path: Path):
    fake = Fake115()
    sync = make(tmp_path, fake)
    real = fake.handler

    def broken(request):
        if request.url.path == "/files" and request.url.params.get("cur") == "0":
            return httpx.Response(200, json={"state": False, "errNo": 20004, "error": "参数错误"})
        return real(request)

    sync.p115._client._transport = httpx.MockTransport(broken)
    r = sync.run(FULL)
    assert r.strm_created == 2 and not r.errors
    assert any("改成逐層列目錄" in n for n in r.notes)
    assert listings(fake)


def test_unreadable_tree_falls_back_to_walk(tmp_path: Path):
    fake = Fake115()
    sync = make(tmp_path, fake)
    real = fake.handler

    def garbage(request):
        if request.url.host == "cdn.115.test":
            return httpx.Response(200, content="<html>not a tree</html>".encode("utf-16"))
        return real(request)

    sync.p115._client._transport = httpx.MockTransport(garbage)
    r = sync.run(FULL)
    assert r.strm_created == 2 and not r.errors
    assert any("改成逐層列目錄" in n for n in r.notes)
    assert fake.deleted  # 看不懂也要把 115 根目錄的目錄樹檔案刪掉
