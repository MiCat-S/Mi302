"""補全缺集：列出媒體庫裡的劇和集號空洞、替每一季向 MoviePilot 建訂閱。"""

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.moviepilot import MoviePilot, library_series
from embyserver.strm_sync import SyncResult

from test_moviepilot import make_config, touch

TVSHOW_NFO = '<tvshow><title>Show A</title><year>2020</year><uniqueid type="tmdb" default="true">4321</uniqueid></tvshow>'


def build(tmp_path: Path, **mp):
    touch(tmp_path / "tv" / "Show A (2020)" / "tvshow.nfo", TVSHOW_NFO)
    for ep in ("S01E01", "S01E02", "S01E04", "S02E01"):
        touch(tmp_path / "tv" / "Show A (2020)" / f"{ep}.strm", "http://x/a.mkv")
    touch(tmp_path / "tv" / "Show B" / "S01E01.strm", "http://x/b.mkv")  # 還沒刮削，沒有 tmdbid
    app = create_app(make_config(tmp_path, **mp), scan_on_start=False)
    app.state.scanner.scan_all()
    return app


def test_library_series_lists_gaps(tmp_path: Path):
    db = build(tmp_path).state.db
    items, total = library_series(db)
    assert total == 2 and items[0]["name"] == "Show A"  # 有空洞的排前面
    a, b = items
    assert (a["tmdbid"], a["year"], a["library"], a["gaps"]) == (4321, 2020, "劇集", 1)
    assert a["seasons"] == [{"season": 1, "count": 3, "gaps": [3]}, {"season": 2, "count": 1, "gaps": []}]
    assert b["tmdbid"] is None and b["seasons"] == [{"season": 1, "count": 1, "gaps": []}]
    assert [s["name"] for s in library_series(db, query="b")[0]] == ["Show B"]
    assert [s["name"] for s in library_series(db, query="2020")[0]] == ["Show A"]  # 年份也搜得到
    assert [s["name"] for s in library_series(db, gaps_only=True)[0]] == ["Show A"]
    assert library_series(db, limit=1) == ([a], 2)
    assert library_series(db, limit=1, offset=1) == ([b], 2)


def test_fill_subscribes_each_season_with_login(tmp_path: Path):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path == "/api/v1/login/access-token":
            return httpx.Response(200, json={"access_token": "jwt", "token_type": "bearer"})
        if request.headers.get("authorization") != "Bearer jwt":
            return httpx.Response(401, json={"detail": "Not authenticated"})  # 建訂閱不接受 API 令牌
        if json.loads(request.content)["season"] == 2:
            return httpx.Response(200, json={"success": False, "message": "Show A (2020) 媒体库中已存在"})
        return httpx.Response(200, json={"success": True, "message": "订阅成功", "data": {"id": 7}})

    cfg = make_config(tmp_path, username="cat", password="pw")
    mp = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(handler))
    series = [
        {"id": 1, "name": "Show A", "year": 2020, "tmdbid": 4321,
         "seasons": [{"season": 1, "count": 3, "gaps": [3]}, {"season": 2, "count": 1, "gaps": []}]},
        {"id": 2, "name": "Show B", "year": None, "tmdbid": None, "seasons": [{"season": 1, "count": 1, "gaps": []}]},
    ]
    r = mp.fill(series, "manual")
    assert (r.total, r.done, r.created, r.complete, r.skipped, r.failed) == (2, 2, 1, 1, 1, 0) and not r.errors
    posts = [json.loads(q.content) for q in sent if q.url.path == "/api/v1/subscribe/" and q.headers.get("authorization")]
    assert posts == [
        {"name": "Show A", "year": "2020", "type": "电视剧", "tmdbid": 4321, "season": 1},
        {"name": "Show A", "year": "2020", "type": "电视剧", "tmdbid": 4321, "season": 2},
    ]
    assert r.details == ["Show B：沒有 tmdbid，略過（先刮削）", "Show A S01：订阅成功", "Show A S02：Show A (2020) 媒体库中已存在"]

    # 只有 API 令牌、沒有帳號密碼：說清楚要填什麼，不送
    only_token = MoviePilot(make_config(tmp_path).moviepilot, cfg, transport=httpx.MockTransport(handler))
    r = only_token.fill(series[:1], "manual")
    assert r.total == 0 and "帳號密碼" in r.errors[0]


def test_fill_stops_on_auth_error(tmp_path: Path):
    cfg = make_config(tmp_path, username="cat", password="wrong")
    mp = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    show = {"id": 1, "name": "A", "year": None, "tmdbid": 1, "seasons": [{"season": s, "count": 1, "gaps": []} for s in (1, 2, 3)]}
    r = mp.fill([show], "manual")
    assert (r.total, r.done, r.failed) == (3, 0, 3) and len(r.errors) == 1


def test_fill_endpoints(tmp_path: Path):
    app = build(tmp_path, username="cat", password="pw")
    c = TestClient(app)
    token = c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]
    h = {"X-Emby-Token": token}
    r = c.get("/web/api/series", params={"q": "show a"}, headers=h).json()
    assert r["total"] == 1 and not r["more"] and r["items"][0]["seasons"][0]["gaps"] == [3]
    page = c.get("/web/api/series", params={"limit": 1}, headers=h).json()
    assert (len(page["items"]), page["total"], page["more"]) == (1, 2, True)
    assert c.get("/web/api/series", params={"limit": 1, "offset": 1}, headers=h).json()["more"] is False
    assert c.get("/web/api/series", params={"gaps": "1"}, headers=h).json()["total"] == 1

    calls = []
    app.state.moviepilot.fill_in_background = lambda shows, source: calls.append(([s["name"] for s in shows], source)) or True
    assert c.post("/web/api/moviepilot/fill", json={"series": [r["items"][0]["id"]]}, headers=h).json()["started"]
    assert c.post("/web/api/moviepilot/fill", json={}, headers=h).json()["started"]  # 全部：只送有 tmdbid 的
    assert calls == [(["Show A"], "manual"), (["Show A"], "manual")]
    status = c.get("/web/api/moviepilot/status", headers=h).json()
    assert status["can_subscribe"] is True and status["fill"]["running"] is False

    # 沒填帳號密碼：不送，說清楚原因
    app2 = build(tmp_path / "nologin")
    c2 = TestClient(app2)
    token2 = c2.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]
    resp = c2.post("/web/api/moviepilot/fill", json={}, headers={"X-Emby-Token": token2})
    assert resp.status_code == 400 and "帳號" in resp.text
    assert c2.get("/web/api/moviepilot/status", headers={"X-Emby-Token": token2}).json()["can_subscribe"] is False


def test_full_sync_can_trigger_fill(tmp_path: Path):
    app = build(tmp_path, username="cat", password="pw", fill_after_full_sync=True)
    calls = []
    app.state.moviepilot.fill_in_background = lambda shows, source: calls.append(([s["name"] for s in shows], source)) or True
    app.state.strm_sync.on_done(SyncResult(mode="incremental"))
    assert calls == []
    app.state.strm_sync.on_done(SyncResult(mode="full"))
    assert calls == [(["Show A"], "sync")]

    # 設定檔要能寫出、讀回這個選項
    from embyserver import config_file, settings
    assert "fill_after_full_sync: true" in config_file.render(app.state.config)
    assert settings.export_settings(app.state.config)["moviepilot"]["fill_after_full_sync"] is True
