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
        return httpx.Response(200, json={"success": True, "message": "ok"})

    cfg = make_config(tmp_path, path_mappings=[{"from": str(tmp_path), "to": "/mp"}])
    done = []
    mp = MoviePilot(cfg.moviepilot, cfg, on_done=lambda: done.append(1), transport=httpx.MockTransport(handler))
    movie = touch(tmp_path / "movies" / "A (2020)" / "A (2020).strm")
    ep = touch(tmp_path / "tv" / "Show" / "S01E01.strm")
    r = mp.scrape([movie, ep], "sync")
    assert (r.total, r.done, r.failed) == (2, 2, 0) and done == [1]

    first = json.loads(sent[0].content)
    assert sent[0].url.path == "/api/v1/media/scrape/local"
    assert sent[0].headers["x-api-key"] == "tok" and sent[0].url.params["token"] == "tok"
    assert first == {
        "storage": "local", "type": "file", "path": "/mp/movies/A (2020)/A (2020).strm",
        "name": "A (2020).strm", "basename": "A (2020)", "extension": "strm",
    }
    second = json.loads(sent[1].content)
    assert second["type"] == "dir" and second["path"] == "/mp/tv/Show/"


def test_scrape_falls_back_to_login_for_old_moviepilot(tmp_path: Path):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path == "/api/v1/login/access-token":
            assert b"username=cat" in request.content
            return httpx.Response(200, json={"access_token": "jwt", "token_type": "bearer"})
        if request.headers.get("authorization") == "Bearer jwt":
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
    assert ok.test() == {"ok": True, "message": "連線成功（MoviePilot 回應：刮削路径无效）"}

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
