"""115 上內容完全相同的影片：找出來、建議保留、刪掉多的，本機 strm 和觀看紀錄跟著處理。"""

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.p115 import P115Service
from embyserver.strm_sync import FULL

from fakes import SHA_EP, SHA_MOVIE, T0, Fake115, build_dupes, wait_dupes



def test_find_and_delete_exact_duplicates(tmp_path: Path):
    app, fake, media, c, h = build_dupes(tmp_path)
    copy = media / "電影" / "Old Movie Copy" / "Old Movie (2001).strm"
    keeper = media / "電影" / "Old Movie (2001).strm"
    (copy.parent / "Old Movie (2001).nfo").write_text("<movie/>")
    # 有人看完了要刪的那份
    db = app.state.db
    copy_item = db.one("SELECT id FROM items WHERE path=?", (str(copy),))["id"]
    uid = db.one("SELECT id FROM users")["id"]
    db.execute("INSERT INTO user_data(user_id, item_id, played, play_count, last_played) VALUES(?,?,1,1,'2026-09-01')", (uid, copy_item))

    assert c.get("/web/api/dupes", headers=h).json()["default_roots"] == ["/影視"]
    assert c.post("/web/api/dupes/scan", json={"paths": ["/"]}, headers=h).json()["started"]
    job = wait_dupes(app)
    assert not job.errors and job.listed == 7
    s = c.get("/web/api/dupes", headers=h).json()
    assert (s["groups"], s["files"], s["reclaimable"], s["roots"]) == (2, 4, 1_800_000_000, ["/"])

    groups = c.get("/web/api/dupes/groups", headers=h).json()
    assert groups["total"] == 2
    movie = next(g for g in groups["items"] if g["sha1"] == SHA_MOVIE)
    assert [(m["file_id"], m["keep"], m["path"]) for m in movie["members"]] == [
        (1, True, "/影視/電影/Old Movie (2001).mkv"), (3, False, "/影視/電影/Old Movie Copy/Old Movie (2001).mkv")]
    assert movie["members"][1]["watched"] and movie["members"][1]["local"] == str(copy)
    ep = next(g for g in groups["items"] if g["sha1"] == SHA_EP)
    assert [(m["file_id"], m["keep"], m["path"], m["local"]) for m in ep["members"]][1] == (
        8, False, "/待整理/Dark.S01E01.mkv", None)
    assert c.get("/web/api/dupes/groups", params={"q": "待整理"}, headers=h).json()["total"] == 1
    # 搜尋時，照建議刪的只算符合搜尋的那幾組：畫面上看到哪些，刪的就是哪些（以前不管搜尋，全部都刪）
    count = lambda **b: c.post("/web/api/dupes/delete", json={"dry_run": True, **b}, headers=h).json()["count"]  # noqa: E731
    assert (count(), count(q="待整理"), count(q="沒有這個檔名")) == (2, 1, 0)

    # 每組至少留一份
    r = c.post("/web/api/dupes/delete", json={"overrides": {"1": True}}, headers=h)
    assert r.status_code == 400 and "至少要留一份" in r.text

    # 先試算：不刪，只回傳會刪幾個、多大
    deleted_before = list(fake.deleted)
    r = c.post("/web/api/dupes/delete", json={"dry_run": True}, headers=h).json()
    assert (r["started"], r["count"], r["size"]) == (False, 2, 1_800_000_000) and fake.deleted == deleted_before

    # 照建議刪：115 上送進回收站、本機的 strm 和 nfo 跟著刪、觀看紀錄轉到保留的那份
    r = c.post("/web/api/dupes/delete", json={}, headers=h).json()
    assert (r["started"], r["count"], r["size"]) == (True, 2, 1_800_000_000)
    job = wait_dupes(app)
    assert not job.errors and (job.done, job.freed) == (2, 1_800_000_000)
    assert sorted(fake.deleted[-2:]) == ["3", "8"]
    assert not copy.exists() and not copy.parent.exists() and keeper.exists()
    assert db.one("SELECT COUNT(*) AS c FROM items WHERE path=?", (str(copy),))["c"] == 0
    keep_item = db.one("SELECT id FROM items WHERE path=?", (str(keeper),))["id"]
    assert db.one("SELECT played FROM user_data WHERE user_id=? AND item_id=?", (uid, keep_item))["played"] == 1
    assert c.get("/web/api/dupes", headers=h).json()["groups"] == 0
    log = c.get("/web/api/dupes/log", headers=h).json()
    assert {e["path"] for e in log} == {"/影視/電影/Old Movie Copy/Old Movie (2001).mkv", "/待整理/Dark.S01E01.mkv"}

    # 之後的同步不會把刪掉的再產生回來
    assert app.state.strm_sync.run(FULL).strm_created == 0 and not copy.exists()

    # 清單過期：找重複之後，建議保留的那份在別的地方（115 App、MoviePilot）被刪了。照舊清單刪下去會一份都不剩，
    # 所以刪之前向 115 重新列一次，要保留的那份不在了就不刪
    fake.files.append({"fid": 20, "cid": 200, "n": "Old Movie (2001).mkv", "pc": "h" * 17, "s": 900_000_000,
                       "te": T0 + 70, "sha": SHA_MOVIE})
    assert c.post("/web/api/dupes/scan", json={"paths": ["/"]}, headers=h).json()["started"]
    wait_dupes(app)
    assert [(r["file_id"], r["keep"]) for r in db.query("SELECT file_id, keep FROM dup_files ORDER BY file_id")] == [(1, 1), (20, 0)]
    fake.files = [f for f in fake.files if f["fid"] != 1]
    assert c.post("/web/api/dupes/delete", json={}, headers=h).json()["started"]
    job = wait_dupes(app)
    assert "20" not in fake.deleted and job.done == 0 and "清單過期" in job.errors[0]


def test_override_and_single_group(tmp_path: Path):
    """可以改成保留別份；也可以只處理某一組。"""
    app, fake, media, c, h = build_dupes(tmp_path)
    c.post("/web/api/dupes/scan", json={"paths": ["/"]}, headers=h)
    wait_dupes(app)
    # 電影那一組：改成留複本、刪原本那份
    r = c.post("/web/api/dupes/delete", json={"sha1": SHA_MOVIE, "size": 900_000_000,
                                              "overrides": {"1": True, "3": False}}, headers=h).json()
    assert r["count"] == 1
    wait_dupes(app)
    assert fake.deleted[-1:] == ["1"]
    assert not (media / "電影" / "Old Movie (2001).strm").exists()
    assert (media / "電影" / "Old Movie Copy" / "Old Movie (2001).strm").exists()
    assert c.get("/web/api/dupes", headers=h).json()["groups"] == 1  # 劇集那一組還在

    # 刪除要 cookie 登入；沒登入不能找，但網頁上的數量（試算）照樣算得出來
    app.state.p115.logout()
    assert c.post("/web/api/dupes/delete", json={}, headers=h).status_code == 400
    assert c.post("/web/api/dupes/delete", json={"dry_run": True}, headers=h).json()["count"] == 1
    # 「全部取消勾選」：完全相同的也可以預設不刪
    assert c.post("/web/api/dupes/delete", json={"dry_run": True, "use_suggestions": False}, headers=h).json()["count"] == 0
    assert c.post("/web/api/dupes/scan", json={}, headers=h).status_code == 400


def build_versions(tmp_path: Path):
    """同一集的 1080p 和 2160p、同一部電影的兩個版本，加上不該算的：加長版、分段檔、一個檔案兩集。"""
    fake = Fake115()
    fake.dirs.update({104: ("Old Movie 4K", 101), 105: ("Old Movie Extended", 101)})
    fake.file(1)["sha"] = SHA_MOVIE
    fake.file(2)["sha"] = SHA_EP
    fake.files += [
        {"fid": 12, "cid": 103, "n": "Dark.S01E01.2160p.HDR.mkv", "pc": "h" * 17, "s": 4_000_000_000, "te": T0 + 70, "sha": "D" * 40},
        {"fid": 13, "cid": 104, "n": "Old.Movie.2001.2160p.mkv", "pc": "i" * 17, "s": 3_000_000_000, "te": T0 + 80, "sha": "E" * 40},
        {"fid": 14, "cid": 105, "n": "Old.Movie.2001.Extended.1080p.mkv", "pc": "j" * 17, "s": 1_000_000_000, "te": T0 + 90, "sha": "F" * 40},
        {"fid": 15, "cid": 103, "n": "Dark.S01E02-E03.mkv", "pc": "k" * 17, "s": 900_000_000, "te": T0 + 91, "sha": "1" * 40},
        {"fid": 16, "cid": 103, "n": "Dark.S01E02.mkv", "pc": "l" * 17, "s": 800_000_000, "te": T0 + 92, "sha": "2" * 40},
    ]
    media = tmp_path / "media"
    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")},
        "users": [{"name": "admin", "password": "pw", "admin": True}],
        "libraries": [
            {"name": "電影", "type": "movies", "paths": [str(media / "電影")]},
            {"name": "劇集", "type": "tvshows", "paths": [str(media / "劇集")]},
        ],
        "p115": {"cookies": "UID=1", "strm": {"tasks": [{"remote": "/影視", "local": str(media)}], "request_delay": 0,
                                               "scan_after_sync": False}},
    }), scan_on_start=False)
    svc = P115Service(app.state.db, initial_cookies="UID=1", transport=httpx.MockTransport(fake.handler))
    for holder in (app.state, app.state.strm_sync, app.state.dupes, app.state.prober, app.state.redirector):
        holder.p115 = svc
    svc.download_url = lambda pc, ua="": f"https://cdn.115.test/{pc}"
    svc.export_poll = 0
    app.state.strm_sync.run(FULL)
    # 兩部電影都寫上同一個 tmdbid（MoviePilot 刮削後的樣子）
    for folder in (media / "電影", media / "電影" / "Old Movie 4K", media / "電影" / "Old Movie Extended"):
        for strm in folder.glob("*.strm"):
            strm.with_suffix(".nfo").write_text(
                "<movie><title>Old Movie</title><year>2001</year><uniqueid type='tmdb'>555</uniqueid></movie>", encoding="utf-8")
    app.state.scanner.scan_all()
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    return app, fake, media, c, h


def test_versions_are_grouped_and_deleted_only_when_picked(tmp_path: Path):
    from embyserver.mediainfo import MediaInfoStore

    app, fake, media, c, h = build_versions(tmp_path)
    db = app.state.db
    # 1080p 那一集有媒體資訊（寬 1920），2160p 那一集只能從檔名看
    ep1080 = media / "劇集" / "Dark" / "Dark.S01E01.strm"
    MediaInfoStore(db).put(str(ep1080), {"source": {"MediaStreams": [
        {"Type": "Video", "Width": 1920, "Height": 1080, "Codec": "h264", "VideoRange": "SDR"},
        {"Type": "Audio", "Codec": "aac", "ChannelLayout": "stereo"}, {"Type": "Subtitle"}]}}, 0, "ffprobe")
    uid = db.one("SELECT id FROM users")["id"]
    ep_item = db.one("SELECT id FROM items WHERE path=?", (str(ep1080),))["id"]
    db.execute("INSERT INTO user_data(user_id, item_id, played, position_ticks, last_played) VALUES(?,?,0,600000000,'2026-09-02')",
               (uid, ep_item))

    c.post("/web/api/dupes/scan", json={}, headers=h)
    job = wait_dupes(app)
    assert not job.errors
    s = c.get("/web/api/dupes", headers=h).json()
    assert s["versions"]["groups"] == 2 and s["versions"]["files"] == 4
    # 這個測試照「解析度最高」建議（預設是 1080P 優先，見下一個測試）；換偏好時已找到的結果當場重算
    assert c.post("/web/api/dupes/prefer", json={"prefer": "highest"}, headers=h).json()["prefer"] == "highest"

    r = c.get("/web/api/dupes/groups", params={"kind": "versions"}, headers=h).json()
    by_title = {g["title"]: g for g in r["items"]}
    assert set(by_title) == {"Dark S01E01", "Old Movie (2001)"}  # 加長版、分段、一檔兩集都不算
    ep = by_title["Dark S01E01"]
    assert [(m["file_id"], m["keep"]) for m in ep["members"]] == [(12, True), (2, False)]  # 2160p 建議保留
    q12, q2 = ep["members"][0]["quality"], ep["members"][1]["quality"]
    assert (q12["res"], q12["hdr"], q12["from"]) == (2160, "HDR", "filename")
    assert (q2["res"], q2["codec"], q2["audio"], q2["subtitles"], q2["from"]) == (1080, "H264", "AAC stereo", 1, "mediainfo")
    assert ep["members"][1]["watched"]
    movie = by_title["Old Movie (2001)"]
    assert [m["file_id"] for m in movie["members"]] == [13, 1]  # 解析度高的在前

    # 不同版本預設不勾：不勾就什麼都不刪
    assert c.post("/web/api/dupes/delete", json={"kind": "versions", "dry_run": True}, headers=h).json()["count"] == 0
    r = c.post("/web/api/dupes/delete", json={"kind": "versions", "use_suggestions": True, "dry_run": True}, headers=h).json()
    assert (r["count"], r["size"]) == (2, 1_800_000_000)
    r = c.post("/web/api/dupes/delete", json={"kind": "versions", "overrides": {"12": True, "2": True}}, headers=h)
    assert r.status_code == 400 and "至少要留一份" in r.text

    # 只刪那一集的 1080p：觀看進度轉到 2160p
    r = c.post("/web/api/dupes/delete", json={"kind": "versions", "grp": ep["grp"], "use_suggestions": True}, headers=h).json()
    assert (r["started"], r["count"]) == (True, 1)
    job = wait_dupes(app)
    assert not job.errors and fake.deleted[-1:] == ["2"]
    assert not ep1080.exists() and (media / "劇集" / "Dark" / "Dark.S01E01.2160p.HDR.strm").exists()
    kept = db.one("SELECT id FROM items WHERE path=?", (str(media / "劇集" / "Dark" / "Dark.S01E01.2160p.HDR.strm"),))["id"]
    assert db.one("SELECT position_ticks FROM user_data WHERE user_id=? AND item_id=?", (uid, kept))["position_ticks"] == 600000000
    s = c.get("/web/api/dupes", headers=h).json()
    assert s["versions"]["groups"] == 1  # 電影那一組還在
    assert c.get("/web/api/dupes/groups", params={"kind": "versions", "q": "Old Movie"}, headers=h).json()["total"] == 1


def test_res_rank_prefers_1080_then_higher_then_lower():
    from embyserver.dupes import res_rank

    order = lambda prefer: sorted([720, 2160, None, 1080, 480], key=lambda r: res_rank(r, prefer))  # noqa: E731
    assert order("1080") == [1080, 2160, 720, 480, None]  # 沒有 1080P 再 4K，再沒有就留剩下最高的
    assert order("2160") == [2160, 1080, 720, 480, None]
    assert order("highest") == [2160, 1080, 720, 480, None]


def test_version_suggestion_follows_preference(tmp_path: Path):
    from embyserver.dupes import PREFER_APPLIED_KEY, DupeFinder
    from embyserver.mediainfo import MediaInfoStore

    app, fake, media, c, h = build_versions(tmp_path)
    # 那一集的 1080p 檔名看不出解析度，靠媒體資訊（寬 1920）；看不出解析度的排最後
    MediaInfoStore(app.state.db).put(str(media / "劇集" / "Dark" / "Dark.S01E01.strm"), {"source": {"MediaStreams": [
        {"Type": "Video", "Width": 1920, "Height": 1080}]}}, 0, "ffprobe")
    c.post("/web/api/dupes/scan", json={}, headers=h)
    assert not wait_dupes(app).errors
    db = app.state.db

    def kept():
        r = c.get("/web/api/dupes/groups", params={"kind": "versions"}, headers=h).json()
        return {g["title"]: next(m["file_id"] for m in g["members"] if m["keep"]) for g in r["items"]}

    # 預設 1080P 優先：那一集留 1080p；電影沒有 1080p，留 4K（另一份看不出解析度）
    assert c.get("/web/api/dupes", headers=h).json()["prefer"] == "1080"
    assert kept() == {"Dark S01E01": 2, "Old Movie (2001)": 13}
    r = c.post("/web/api/dupes/delete", json={"kind": "versions", "use_suggestions": True, "dry_run": True}, headers=h).json()
    assert (r["count"], r["size"]) == (2, 4_900_000_000)  # 全部照建議勾選：刪 2160p 那集和看不出解析度的電影
    assert c.post("/web/api/dupes/prefer", json={"prefer": "2160"}, headers=h).status_code == 200
    assert kept() == {"Dark S01E01": 12, "Old Movie (2001)": 13}
    assert c.post("/web/api/dupes/prefer", json={"prefer": "720"}, headers=h).status_code == 400

    # 舊版照「最高解析度」算好的結果：升級後第一次啟動照新的偏好（預設 1080P）重算
    db.set_meta("dupes_prefer", "")
    db.set_meta(PREFER_APPLIED_KEY, "")
    DupeFinder(db, app.state.p115, app.state.strm_sync, app.state.scanner)
    assert kept() == {"Dark S01E01": 2, "Old Movie (2001)": 13} and db.get_meta(PREFER_APPLIED_KEY) == "1080"

    # 找重複或刪除進行中不能換
    app.state.dupes._lock.acquire()
    try:
        assert c.post("/web/api/dupes/prefer", json={"prefer": "highest"}, headers=h).status_code == 409
    finally:
        app.state.dupes._lock.release()


def test_versions_need_known_matching_episode_numbers(tmp_path: Path):
    """不知道第幾集的（nfo 寫 -1）不能算同一集的不同版本；檔名和媒體庫的集號對不上的也不列。"""
    app, fake, media, c, h = build_versions(tmp_path)
    fake.dirs.update({106: ("中国新说唱 (2017)", 102), 107: ("Season 01", 106)})
    shows = ["10.潘玮柏战队面临团危机-蓝光4K", "03-比赛惊现死亡之组-蓝光4K", "01-嘻哈首战-蓝光4K", "无法识别的特辑A", "无法识别的特辑B"]
    for i, stem in enumerate(shows):
        fake.files.append({"fid": 20 + i, "cid": 107, "n": f"{stem}.mp4", "pc": chr(ord("m") + i) * 17,
                           "s": 2_000_000_000 + i, "te": T0 + 100 + i, "sha": str(i) * 40})
    # 檔名是第 2 集，nfo 卻寫第 1 集：和第 1 集的兩個版本不能放在一起
    fake.files.append({"fid": 30, "cid": 103, "n": "Dark.S01E02.WEB.mkv", "pc": "w" * 17, "s": 700_000_000, "te": T0 + 200, "sha": "3" * 40})
    app.state.strm_sync.run(FULL)
    for stem in shows:
        (media / "劇集" / "中国新说唱 (2017)" / "Season 01" / f"{stem}.nfo").write_text(
            "<episodedetails><season>-1</season><episode>-1</episode></episodedetails>", encoding="utf-8")
    (media / "劇集" / "Dark" / "Dark.S01E02.WEB.nfo").write_text(
        "<episodedetails><season>1</season><episode>1</episode></episodedetails>", encoding="utf-8")
    app.state.scanner.scan_all()

    c.post("/web/api/dupes/scan", json={}, headers=h)
    assert not wait_dupes(app).errors
    r = c.get("/web/api/dupes/groups", params={"kind": "versions"}, headers=h).json()
    by_title = {g["title"]: g for g in r["items"]}
    assert set(by_title) == {"Dark S01E01", "Old Movie (2001)"}
    assert sorted(m["file_id"] for m in by_title["Dark S01E01"]["members"]) == [2, 12]


def test_old_version_results_are_cleared_after_rule_change(tmp_path: Path):
    from embyserver.dupes import VERSIONS_RULE_KEY, DupeFinder

    app, fake, media, c, h = build_versions(tmp_path)
    c.post("/web/api/dupes/scan", json={}, headers=h)
    assert not wait_dupes(app).errors
    db = app.state.db
    assert db.one("SELECT COUNT(*) AS n FROM dup_versions")["n"] == 4
    # 模擬舊版留下的結果：規則版本還是舊的，重新啟動時清掉，並提醒重新找
    db.set_meta(VERSIONS_RULE_KEY, "1")
    db.execute("INSERT INTO dup_files(file_id, sha1, size, name, keep) VALUES(99, 'X', 1, 'a.mkv', 1)")
    finder = DupeFinder(db, app.state.p115, app.state.strm_sync, app.state.scanner)
    assert db.one("SELECT COUNT(*) AS n FROM dup_versions")["n"] == 0
    assert finder.summary()["versions_outdated"]
    assert db.one("SELECT COUNT(*) AS n FROM dup_files")["n"] == 1  # 完全相同的不受影響
    # 同一個版本再啟動一次不會再清
    DupeFinder(db, app.state.p115, app.state.strm_sync, app.state.scanner)
    assert finder.summary()["versions_outdated"]
    # 重新找過就好了
    app.state.dupes = finder
    c.post("/web/api/dupes/scan", json={}, headers=h)
    assert not wait_dupes(app).errors
    s = c.get("/web/api/dupes", headers=h).json()
    assert not s["versions_outdated"] and s["versions"]["groups"] == 2


def test_name_complete():
    from embyserver.dupes import name_complete

    assert name_complete("Dark.S01E02.1080p.mkv", "episode") and name_complete("Dark 1x02.mkv", "episode")
    for name in ("10.潘玮柏战队.mp4", "Dark EP02.mkv", "某剧 第2集.mp4", "Dark.mkv"):
        assert not name_complete(name, "episode"), name
    assert name_complete("Old Movie (2001).mkv", "movie") and name_complete("Old.Movie.2001.2160p.mkv", "movie")
    assert not name_complete("old movie 2160p.mkv", "movie") and not name_complete("heat 1080p.mkv", "movie")
    # 不知道類型（完全相同的檔案）：看得出集號就要標準寫法，看不出就要有年份
    assert name_complete("Dark.S01E02.mkv") and not name_complete("02-xx 2019.mkv") and name_complete("Heat (1995).mkv")


def test_suggestion_prefers_complete_names_then_smaller_files(tmp_path: Path):
    from embyserver.db import Database
    from embyserver.dupes import SUGGEST_RULE_KEY, DupeFinder

    db = Database(str(tmp_path / "t.db"))
    DupeFinder(db, None, None, None)  # 新資料庫：記下目前的規則版本
    GB = 1_000_000_000
    rows = [  # (file_id, grp, name, size, res)
        (1, "ep:1:1:5", "Show.S01E05.1080p.BluRay.mkv", 8 * GB, 1080),
        (2, "ep:1:1:5", "05.1080p.mp4", 1 * GB, 1080),  # 最小，但檔名只有集號
        (3, "ep:1:1:5", "Show.S01E05.1080p.WEB.mkv", 2 * GB, 1080),  # 檔名完整裡最小的 → 建議保留
        (4, "ep:1:1:5", "Show.S01E05.2160p.mkv", 1.5 * GB, 2160),  # 預設 1080P 優先，4K 不留
        (5, "movie:tmdb:9", "heat.1080p.mkv", 2 * GB, 1080),
        (6, "movie:tmdb:9", "Heat (1995) 1080p.mkv", 5 * GB, 1080),  # 有年份 → 建議保留
        (7, "movie:tmdb:8", "Up (2009) 1080p REMUX.mkv", 30 * GB, 1080),
        (8, "movie:tmdb:8", "Up (2009) 1080p.mkv", 4 * GB, 1080),  # 都完整：留小的
    ]
    db.executemany(
        "INSERT INTO dup_versions(file_id, grp, title, name, size, mtime, quality, keep) VALUES(?,?,?,?,?,?,?,0)",
        [(fid, grp, grp, name, int(size), 100 + fid, json.dumps({"res": res})) for fid, grp, name, size, res in rows],
    )
    # 完全相同：兩份都有 strm；早上傳的那份檔名只有集號
    db.executemany("INSERT INTO dup_files(file_id, sha1, size, name, local, mtime, keep) VALUES(?,?,?,?,?,?,0)",
                   [(20, "A", 10, "02.mkv", "/a/02.strm", 1), (21, "A", 10, "Show.S01E02.mkv", "/b/x.strm", 5),
                    (22, "A", 10, "Show.S01E02.mkv", None, 0)])  # 沒有 strm 的不優先，就算檔名完整又最早
    db.set_meta(SUGGEST_RULE_KEY, "1")  # 舊版照「檔案大的」算好的結果
    DupeFinder(db, None, None, None)  # 規則版本不同：啟動時重算
    kept = {r["file_id"] for r in db.query("SELECT file_id FROM dup_versions WHERE keep=1")}
    assert kept == {3, 6, 8}
    assert [r["file_id"] for r in db.query("SELECT file_id FROM dup_files WHERE keep=1")] == [21]
    assert db.get_meta(SUGGEST_RULE_KEY) == "2"


def test_big_files_are_deleted_only_when_picked(tmp_path: Path):
    """大檔案：找重複時順便記下 1 GB 以上的影片，照大小、種類篩選。只刪勾了的，「符合條件的全部」只算條件內的；
    刪掉的那一集還有別的版本時，觀看紀錄轉過去。"""
    app, fake, media, c, h = build_versions(tmp_path)
    db = app.state.db
    ep4k = media / "劇集" / "Dark" / "Dark.S01E01.2160p.HDR.strm"
    ep1080 = media / "劇集" / "Dark" / "Dark.S01E01.strm"
    uid = db.one("SELECT id FROM users")["id"]
    db.execute("INSERT INTO user_data(user_id, item_id, played, play_count, last_played) VALUES(?,?,1,1,'2026-09-03')",
               (uid, db.one("SELECT id FROM items WHERE path=?", (str(ep4k),))["id"]))
    c.post("/web/api/dupes/scan", json={}, headers=h)
    assert not wait_dupes(app).errors
    assert c.get("/web/api/dupes", headers=h).json()["big"] == {"files": 2, "size": 7_000_000_000}  # 1 GB 以下的不記

    def big(**params):
        return c.get("/web/api/dupes/groups", params={"kind": "big", **params}, headers=h).json()

    r = big()
    assert [(m["file_id"], m["type"], m["versions"]) for m in r["items"]] == [(12, "Episode", 1), (13, "Movie", 1)]
    assert r["items"][0]["watched"] and r["items"][0]["quality"]["res"] == 2160
    gib = 1024 ** 3
    assert [m["file_id"] for m in big(min_size=3 * gib)["items"]] == [12]
    assert [m["file_id"] for m in big(type="movie")["items"]] == [13]

    def dry(**body):
        return c.post("/web/api/dupes/delete", json={"kind": "big", "dry_run": True, **body}, headers=h).json()

    assert dry()["count"] == 0  # 沒勾就不刪
    assert (dry(use_suggestions=True, min_size=3 * gib)["count"], dry(use_suggestions=True, min_size=3 * gib)["size"]) == \
        (1, 4_000_000_000)
    assert dry(use_suggestions=True, overrides={"13": False})["count"] == 1

    r = c.post("/web/api/dupes/delete", json={"kind": "big", "overrides": {"12": True}}, headers=h).json()
    assert (r["started"], r["count"]) == (True, 1)
    assert not wait_dupes(app).errors and fake.deleted[-1:] == ["12"]
    assert not ep4k.exists() and ep1080.exists()
    kept = db.one("SELECT id FROM items WHERE path=?", (str(ep1080),))["id"]
    assert db.one("SELECT played FROM user_data WHERE user_id=? AND item_id=?", (uid, kept))["played"] == 1
    assert [m["file_id"] for m in big()["items"]] == [13]
    assert c.get("/web/api/dupes/log", headers=h).json()[0]["name"] == "Dark.S01E01.2160p.HDR.mkv"
