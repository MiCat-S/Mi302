"""MoviePilot 刮削串接，以及 MoviePilot 把 Mi302 當 Emby 用到的端點。"""

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import MoviePilotConfig, PathRule, config_from_dict
from embyserver.moviepilot import MoviePilot


def make_config(tmp_path: Path, **mp):
    return config_from_dict(
        {
            "server": {"data_dir": str(tmp_path / "data")},
            "users": [{"name": "admin", "password": "pw", "admin": True}],
            "libraries": [
                {"name": "電影", "type": "movies", "paths": [str(tmp_path / "movies")]},
                {"name": "劇集", "type": "tvshows", "paths": [str(tmp_path / "tv")]},
            ],
            "moviepilot": {"url": "http://mp:3000", "api_token": "tok", **mp},
        }
    )


def touch(path: Path, text: str = "x") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


def writes(request: httpx.Request, base: Path, mp_root: str = "", image: bool = False) -> None:
    """像 MoviePilot 一樣把 nfo（和劇照）寫到送來的路徑旁邊。"""
    body = json.loads(request.content)
    if not body.get("path"):
        return
    p = Path(str(base) + body["path"][len(mp_root):]) if mp_root else Path(body["path"])
    if body["type"] == "dir":
        touch(p / "tvshow.nfo", "<tvshow/>")
    else:
        touch(p.with_suffix(".nfo"), "<episodedetails/>")
        if image:
            touch(p.with_suffix(".jpg"), "img")


def test_plan_skips_scraped_and_groups_new_series(tmp_path: Path):
    cfg = make_config(tmp_path)
    mp = MoviePilot(cfg.moviepilot, cfg)
    new_movie = touch(tmp_path / "movies" / "A (2020)" / "A (2020).strm")
    done_movie = touch(tmp_path / "movies" / "B (2021)" / "B (2021).strm")
    touch(tmp_path / "movies" / "B (2021)" / "movie.nfo")
    ep1 = touch(tmp_path / "tv" / "New Show" / "Season 1" / "S01E01.strm")
    ep2 = touch(tmp_path / "tv" / "New Show" / "Season 1" / "S01E02.strm")
    touch(tmp_path / "tv" / "Old Show" / "tvshow.nfo")
    old_ep = touch(tmp_path / "tv" / "Old Show" / "S01E09.strm")
    scraped_ep = touch(tmp_path / "tv" / "Old Show" / "S01E01.strm")
    touch(tmp_path / "tv" / "Old Show" / "S01E01.nfo")

    plan = mp.plan([new_movie, done_movie, ep1, ep2, old_ep, scraped_ep])
    assert plan == [
        (Path(new_movie), False),
        (tmp_path / "tv" / "New Show", True),  # 新劇整個資料夾刮一次
        (Path(old_ep), False),  # 已刮削過的劇只送新的集
    ]
    # 手動「刮削缺少資料的項目」會掃整個媒體庫
    assert sorted(str(p) for p, _ in mp.plan(mp.missing())) == sorted(
        [new_movie, str(tmp_path / "tv" / "New Show"), old_ep]
    )


def test_scrape_sends_mapped_paths_with_api_token(tmp_path: Path):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        writes(request, tmp_path, "/mp")
        return httpx.Response(200, json={"success": True, "message": "ok"})

    cfg = make_config(tmp_path, path_mappings=[{"from": str(tmp_path), "to": "/mp"}])
    done = []
    mp = MoviePilot(cfg.moviepilot, cfg, on_done=lambda paths: done.append(paths), transport=httpx.MockTransport(handler))
    movie = touch(tmp_path / "movies" / "A (2020)" / "A (2020).strm")
    ep = touch(tmp_path / "tv" / "Show" / "S01E01.strm")
    r = mp.scrape([movie, ep], "sync")
    # 還沒刮削過的劇送整個劇集資料夾；刮好的路徑交給掃描器只掃那些地方
    assert (r.total, r.done, r.failed) == (2, 2, 0) and done == [[movie, str(tmp_path / "tv" / "Show")]]

    # 同時送好幾項，順序不一定
    by_type = {json.loads(q.content)["type"]: q for q in sent}
    first = by_type["file"]
    assert first.url.path == "/api/v1/media/scrape/local"
    assert first.headers["x-api-key"] == "tok" and first.url.params["token"] == "tok"
    assert "media_id" not in first.url.params  # 電影不知道 tmdbid，讓 MoviePilot 自己辨識
    assert json.loads(first.content) == {
        "storage": "local", "type": "file", "path": "/mp/movies/A (2020)/A (2020).strm",
        "name": "A (2020).strm", "basename": "A (2020)", "extension": "strm",
    }
    assert json.loads(by_type["dir"].content)["path"] == "/mp/tv/Show/"


def test_scrape_falls_back_to_login_for_old_moviepilot(tmp_path: Path):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path == "/api/v1/login/access-token":
            assert b"username=cat" in request.content
            return httpx.Response(200, json={"access_token": "jwt", "token_type": "bearer"})
        if request.headers.get("authorization") == "Bearer jwt":
            writes(request, tmp_path)
            return httpx.Response(200, json={"success": True})
        return httpx.Response(401, json={"detail": "Not authenticated"})

    cfg = make_config(tmp_path, username="cat", password="pw")
    mp = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(handler))
    movie = touch(tmp_path / "movies" / "A.strm")
    assert mp.scrape([movie], "manual").done == 1
    assert [r.url.path for r in sent] == [
        "/api/v1/media/scrape/local", "/api/v1/login/access-token", "/api/v1/media/scrape/local",
    ]


def test_test_connection_messages(tmp_path: Path):
    cfg = make_config(tmp_path)

    ok = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"success": False, "message": "刮削路径无效"})
    ))
    res = ok.test()
    assert res["ok"] and res["message"].startswith("連線成功") and "无效" not in res["message"]

    denied = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    res = denied.test()
    assert not res["ok"] and "API 令牌不正確" in res["message"] and "帳號密碼" in res["message"]

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    res = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(down)).test()
    assert not res["ok"] and "連不到 mp" in res["message"]


def test_auth_error_stops_batch(tmp_path: Path):
    cfg = make_config(tmp_path)
    mp = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    paths = [touch(tmp_path / "movies" / f"{i}.strm") for i in range(3)]
    r = mp.scrape(paths, "manual")
    assert (r.total, r.done, r.failed) == (3, 0, 3) and len(r.errors) == 1


def test_new_strm_after_sync_goes_to_moviepilot(tmp_path: Path):
    app = create_app(make_config(tmp_path), scan_on_start=False)
    calls = []
    app.state.moviepilot.scrape = lambda paths, source: calls.append((paths, source)) or type("R", (), {"done": 1})()
    from embyserver.strm_sync import SyncResult

    app.state.strm_sync.on_done(SyncResult(new_files=["/x/a.strm"]))
    app.state.strm_sync.on_done(SyncResult(new_files=[]))  # 沒有新檔案就不送
    assert calls == [(["/x/a.strm"], "sync")]


def test_emby_endpoints_for_moviepilot(tmp_path: Path):
    touch(tmp_path / "movies" / "A (2020)" / "A (2020).strm", "http://x/a.mkv")
    touch(tmp_path / "tv" / "Show" / "S01E01.strm", "http://x/b.mkv")
    app = create_app(make_config(tmp_path), scan_on_start=False)
    app.state.scanner.scan_all()
    c = TestClient(app)
    token = c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]
    key = c.post("/web/api/apikeys", json={"name": "MoviePilot"}, headers={"X-Emby-Token": token}).json()["key"]
    assert c.get("/web/api/apikeys", headers={"X-Emby-Token": token}).json()[0]["name"] == "MoviePilot"

    # MoviePilot 用 api_key 查詢參數、路徑帶 /emby 前綴
    q = {"api_key": key}
    users = c.get("/Users", params=q).json()
    assert users[0]["Policy"]["IsAdministrator"] is True
    folders = c.get("/emby/Library/SelectableMediaFolders", params=q).json()
    assert [f["Name"] for f in folders] == ["電影", "劇集"]
    assert folders[0]["SubFolders"][0]["Path"] == str(tmp_path / "movies")
    vf = c.get("/emby/Library/VirtualFolders/Query", params=q).json()
    assert vf["Items"][1]["LibraryOptions"]["PathInfos"] == [{"Path": str(tmp_path / "tv")}]
    counts = c.get("/emby/Items/Counts", params=q).json()
    assert (counts["MovieCount"], counts["SeriesCount"], counts["EpisodeCount"]) == (1, 1, 1)
    movies = c.get("/emby/Items", params={**q, "IncludeItemTypes": "Movie", "Recursive": "true",
                                          "SearchTerm": "A", "Fields": "ProviderIds,Path"}).json()["Items"]
    assert movies[0]["Name"] == "A" and movies[0]["Path"].endswith("A (2020).strm")
    assert c.post(f"/emby/Items/{movies[0]['Id']}/Refresh", params=q).status_code == 204
    assert c.post("/emby/Library/Media/Updated", params=q).status_code == 204

    c.delete(f"/web/api/apikeys/{key}", headers={"X-Emby-Token": token})
    assert c.get("/emby/Items/Counts", params=q).status_code == 401


def test_missing_path_hints_path_mapping(tmp_path: Path):
    cfg = make_config(tmp_path)
    mp = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"success": False, "message": "刮削路径不存在"})
    ))
    movie = touch(tmp_path / "movies" / "A.strm")
    ok, message = mp.scrape_one(Path(movie), False)
    assert not ok and "路徑對應" in message and movie in message


def test_scrape_runs_items_concurrently(tmp_path: Path):
    import threading
    import time

    lock, state = threading.Lock(), {"now": 0, "max": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.05)
        writes(request, tmp_path)
        with lock:
            state["now"] -= 1
        return httpx.Response(200, json={"success": True})

    cfg = make_config(tmp_path, concurrency=3)
    done = []
    mp = MoviePilot(cfg.moviepilot, cfg, on_done=done.append, transport=httpx.MockTransport(handler))
    movies = [touch(tmp_path / "movies" / f"M{i} (2020)" / f"M{i} (2020).strm") for i in range(6)]
    r = mp.scrape(movies, "manual")
    assert (r.total, r.done, r.failed) == (6, 6, 0)
    assert 2 <= state["max"] <= 3
    assert done == [movies]  # 交給掃描器的順序和送出的一樣


def test_episode_of_scraped_series_sends_tmdbid(tmp_path: Path):
    sent = []

    def handler(request):
        sent.append(request)
        writes(request, tmp_path, image=True)
        return httpx.Response(200, json={"success": True})

    touch(tmp_path / "tv" / "Show" / "tvshow.nfo", '<tvshow><uniqueid type="tmdb">4321</uniqueid></tvshow>')
    ep = touch(tmp_path / "tv" / "Show" / "Season 1" / "Show.S01E02.strm")
    cfg = make_config(tmp_path)
    mp = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(handler))
    assert mp.scrape([ep], "sync").done == 1
    params = sent[0].url.params
    assert (params["media_source"], params["media_id"], params["type_name"]) == ("themoviedb", "4321", "电视剧")
    assert params["token"] == "tok"  # API 令牌照樣帶著


def test_checks_what_moviepilot_actually_wrote(tmp_path: Path):
    wrote = {"image": False, "nothing": False}

    def handler(request):
        if not wrote["nothing"]:
            writes(request, tmp_path, image=wrote["image"])
        return httpx.Response(200, json={"success": True, "message": "刮削完成"})

    touch(tmp_path / "tv" / "Show" / "tvshow.nfo", '<tvshow><uniqueid type="tmdb">1</uniqueid></tvshow>')
    ep1 = touch(tmp_path / "tv" / "Show" / "S01E01.strm")
    ep2 = touch(tmp_path / "tv" / "Show" / "S01E02.strm")
    cfg = make_config(tmp_path)
    mp = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(handler))
    mp.verify_wait = 0

    # 認不出集數時 MoviePilot 什麼都不寫，卻回報完成：算失敗並說明
    wrote["nothing"] = True
    r = mp.scrape([ep1], "sync")
    assert (r.done, r.failed) == (0, 1) and "認不出集數" in r.errors[0]

    # 寫了 nfo 沒有劇照：算成功，另外計數，記下來不再重送
    wrote["nothing"] = False
    r = mp.scrape([ep1], "sync")
    assert (r.done, r.failed, r.no_image) == (1, 0, 1)

    # 手動刮削：有 nfo 沒劇照的集也送，但剛確定沒有劇照的不送；有劇照的不送
    touch(tmp_path / "tv" / "Show" / "S01E02.nfo")
    assert mp.plan([ep1, ep2]) == []
    assert mp.plan([ep1, ep2], with_images=True) == [(Path(ep2), False)]
    wrote["image"] = True
    r = mp.scrape([ep1, ep2], "manual", with_images=True)
    assert (r.total, r.done, r.no_image) == (1, 1, 0)
    assert mp.plan([ep1, ep2], with_images=True) == []


def test_no_image_marks_persist_in_database(tmp_path: Path):
    from embyserver.db import Database

    touch(tmp_path / "tv" / "Show" / "tvshow.nfo")
    ep = touch(tmp_path / "tv" / "Show" / "S01E01.strm")
    touch(tmp_path / "tv" / "Show" / "S01E01.nfo")
    cfg = make_config(tmp_path)
    db = Database(":memory:")
    mp = MoviePilot(cfg.moviepilot, cfg, db=db)
    assert mp.plan([ep], with_images=True) == [(Path(ep), False)]
    mp._mark_no_image(Path(ep))
    assert MoviePilot(cfg.moviepilot, cfg, db=db).plan([ep], with_images=True) == []


def test_concurrency_setting_is_clamped_and_saved(tmp_path: Path):
    from embyserver import config_file, settings

    cfg = make_config(tmp_path)
    assert cfg.moviepilot.concurrency == 3
    settings.apply_settings(cfg, {"moviepilot": {"concurrency": 20}})
    assert cfg.moviepilot.concurrency == 8
    settings.apply_settings(cfg, {"moviepilot": {"concurrency": 0}})
    assert cfg.moviepilot.concurrency == 1
    assert "concurrency: 1" in config_file.render(cfg)


def test_episode_without_still_uses_series_banner(tmp_path: Path):
    show = tmp_path / "tv" / "Show (2020)"
    touch(show / "tvshow.nfo", "<tvshow><title>Show</title></tvshow>")
    touch(show / "landscape.jpg", "banner")
    touch(show / "S01E01.strm", "http://x/a.mkv")
    touch(show / "S01E02.strm", "http://x/b.mkv")
    touch(show / "S01E02.jpg", "still")
    app = create_app(make_config(tmp_path), scan_on_start=False)
    app.state.scanner.scan_all()
    c = TestClient(app)
    token = c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]
    h = {"X-Emby-Token": token}
    series_id = c.get("/Items", params={"IncludeItemTypes": "Series", "Recursive": "true"}, headers=h).json()["Items"][0]["Id"]
    eps = {e["IndexNumber"]: e for e in c.get(f"/Shows/{series_id}/Episodes", headers=h).json()["Items"]}
    assert eps[1]["ImageTags"]["Primary"] == eps[1]["ParentThumbImageTag"]  # 沒有劇照：用劇的橫幅圖
    assert eps[2]["ImageTags"]["Primary"] != eps[1]["ImageTags"]["Primary"]  # 有劇照的用自己的
    assert c.get(f"/Items/{eps[1]['Id']}/Images/Primary").content == b"banner"
    assert c.get(f"/Items/{eps[2]['Id']}/Images/Primary").content == b"still"


def test_plan_finds_series_inside_category_folders(tmp_path: Path):
    """媒體庫路徑選「电视剧」、底下再分「国产剧」時，送的是那一部劇，不是整個分類。"""
    cfg = make_config(tmp_path)
    mp = MoviePilot(cfg.moviepilot, cfg)
    show = tmp_path / "tv" / "国产剧" / "庆余年 (2019)"
    ep1 = touch(show / "Season 1" / "庆余年.S01E01.strm")
    ep2 = touch(show / "Season 1" / "庆余年.S01E02.strm")
    assert mp.plan([ep1, ep2]) == [(show, True)]
    touch(show / "tvshow.nfo", "<tvshow><uniqueid type='tmdb'>94840</uniqueid></tvshow>")
    assert mp.plan([ep1]) == [(Path(ep1), False)]
    assert mp._series_dir(Path(ep1)) == show and mp._episode_tmdbid(Path(ep1)) == "94840"
