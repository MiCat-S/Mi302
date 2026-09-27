"""115 上內容完全相同的影片：找出來、建議保留、刪掉多的，本機 strm 和觀看紀錄跟著處理。"""

import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.p115 import P115Service
from embyserver.strm_sync import FULL

from test_incremental import T0, Fake115

SHA_MOVIE = "A" * 40
SHA_EP = "B" * 40


def build(tmp_path: Path):
    fake = Fake115()
    fake.dirs.update({104: ("Old Movie Copy", 101), 200: ("待整理", 0)})
    fake.file(1)["sha"] = SHA_MOVIE
    fake.file(2)["sha"] = SHA_EP
    fake.files += [
        # 同一部電影轉存了兩次，都在同步目錄裡：兩份都有 strm，早上傳的那份建議保留
        {"fid": 3, "cid": 104, "n": "Old Movie (2001).mkv", "pc": "c" * 17, "s": 900_000_000, "te": T0 + 50, "sha": SHA_MOVIE},
        # 同步目錄外面的一份（待整理）：沒有 strm
        {"fid": 8, "cid": 200, "n": "Dark.S01E01.mkv", "pc": "d" * 17, "s": 900_000_000, "te": T0 + 60, "sha": SHA_EP},
        # 不是影片、或大小不同的，不算重複
        {"fid": 9, "cid": 200, "n": "poster.jpg", "pc": "e" * 17, "s": 10, "te": T0, "sha": "C" * 40},
        {"fid": 10, "cid": 200, "n": "poster copy.jpg", "pc": "f" * 17, "s": 10, "te": T0, "sha": "C" * 40},
        {"fid": 11, "cid": 200, "n": "other.mkv", "pc": "g" * 17, "s": 123, "te": T0, "sha": SHA_MOVIE},
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
    # 換成接假 115 的客戶端（一開始就指定 transport；事後換 _transport 會被環境變數裡的代理繞過去）
    svc = P115Service(app.state.db, initial_cookies="UID=1", transport=httpx.MockTransport(fake.handler))
    for holder in (app.state, app.state.strm_sync, app.state.dupes, app.state.prober, app.state.redirector):
        holder.p115 = svc
    svc.download_url = lambda pc, ua="": f"https://cdn.115.test/{pc}"
    svc.export_poll = 0
    assert app.state.strm_sync.run(FULL).strm_created == 3
    app.state.scanner.scan_all()
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    return app, fake, media, c, h


def wait_job(app):
    for _ in range(200):
        if not app.state.dupes.job.running:
            return app.state.dupes.job
        time.sleep(0.02)
    raise AssertionError("找重複／刪重複沒有結束")


def test_find_and_delete_exact_duplicates(tmp_path: Path):
    app, fake, media, c, h = build(tmp_path)
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
    job = wait_job(app)
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
    job = wait_job(app)
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


def test_override_and_single_group(tmp_path: Path):
    """可以改成保留別份；也可以只處理某一組。"""
    app, fake, media, c, h = build(tmp_path)
    c.post("/web/api/dupes/scan", json={"paths": ["/"]}, headers=h)
    wait_job(app)
    # 電影那一組：改成留複本、刪原本那份
    r = c.post("/web/api/dupes/delete", json={"sha1": SHA_MOVIE, "size": 900_000_000,
                                              "overrides": {"1": True, "3": False}}, headers=h).json()
    assert r["count"] == 1
    wait_job(app)
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
    job = wait_job(app)
    assert not job.errors
    s = c.get("/web/api/dupes", headers=h).json()
    assert s["versions"]["groups"] == 2 and s["versions"]["files"] == 4

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
    job = wait_job(app)
    assert not job.errors and fake.deleted[-1:] == ["2"]
    assert not ep1080.exists() and (media / "劇集" / "Dark" / "Dark.S01E01.2160p.HDR.strm").exists()
    kept = db.one("SELECT id FROM items WHERE path=?", (str(media / "劇集" / "Dark" / "Dark.S01E01.2160p.HDR.strm"),))["id"]
    assert db.one("SELECT position_ticks FROM user_data WHERE user_id=? AND item_id=?", (uid, kept))["position_ticks"] == 600000000
    s = c.get("/web/api/dupes", headers=h).json()
    assert s["versions"]["groups"] == 1  # 電影那一組還在
    assert c.get("/web/api/dupes/groups", params={"kind": "versions", "q": "Old Movie"}, headers=h).json()["total"] == 1
