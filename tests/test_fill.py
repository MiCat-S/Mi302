"""補全缺集：列出媒體庫裡的劇和集號空洞、替每一季向 MoviePilot 建訂閱。"""

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.moviepilot import MoviePilot, library_series
from embyserver.strm_sync import SyncResult

from fakes import make_config, touch

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
    # 還沒對照過 TMDB：只看得出集號的空洞，缺哪幾集（missing）不知道
    unknown = {"tmdb": None, "missing": None, "subscribed": None}
    assert a["seasons"] == [
        {"season": 1, "count": 3, "first": 1, "last": 4, "gaps": [3], **unknown},
        {"season": 2, "count": 1, "first": 1, "last": 1, "gaps": [], **unknown},
    ]
    assert b["tmdbid"] is None and b["seasons"] == [{"season": 1, "count": 1, "first": 1, "last": 1, "gaps": [], **unknown}]
    # 每一部一個狀態（照接下來要做什麼分）：還沒對照、沒有 tmdbid（要先刮削）
    assert (a["state"], a["missing"], a["unchecked"], b["state"]) == ("unchecked", 0, 2, "notmdb")
    assert library_series(db, view="missing") == ([], 0) and library_series(db, view="unchecked") == ([a, b], 2)
    assert [s["name"] for s in library_series(db, query="b")[0]] == ["Show B"]
    assert [s["name"] for s in library_series(db, query="2020")[0]] == ["Show A"]  # 年份也搜得到
    assert [s["name"] for s in library_series(db, gaps_only=True)[0]] == ["Show A"]
    assert [s["name"] for s in library_series(db, year=2020)[0]] == ["Show A"]  # 只看某一年的
    assert library_series(db, year=2021) == ([], 0)
    assert library_series(db, limit=1) == ([a], 2)
    assert library_series(db, limit=1, offset=1) == ([b], 2)

    # 對照過 TMDB（檢查缺集、補全時記下的）：第 1 季播了 6 集、第 7 集還沒播，缺 3、5、6；第 2 季齊全。
    # 沒有空洞的季也可能缺後面幾集，只有對照過才知道；齊全的劇不列在「只看缺集的」裡
    eps = {str(e): "2020-01-01" for e in range(1, 7)} | {"7": "2999-01-01"}
    db.execute("INSERT INTO tmdb_seasons(tmdbid, season, episodes, at) VALUES(4321, 1, ?, 0)", (json.dumps(eps),))
    db.execute("INSERT INTO tmdb_seasons(tmdbid, season, episodes, at) VALUES(4321, 2, ?, 0)", (json.dumps({"1": "2021-01-01"}),))
    stats = {}
    (a,), total = library_series(db, view="missing", stats=stats)
    assert total == 1 and (a["state"], a["missing"], a["unchecked"], a["to_fill"]) == ("missing", 3, 0, 1)
    assert stats == {"missing": 1, "subscribed": 0, "unchecked": 0, "notmdb": 1, "complete": 0, "excluded": 0, "total": 2}
    assert [(s["tmdb"], s["missing"]) for s in a["seasons"]] == [(6, [3, 5, 6]), (1, [])]
    # MoviePilot 已經訂閱了缺集的那一季：不用再補，換到「已訂閱」；標了「不補」的自成一類
    (a,), _ = library_series(db, view="subscribed", subscribed={(4321, 1)})
    assert (a["state"], a["to_fill"], a["seasons"][0]["subscribed"]) == ("subscribed", 0, True)
    assert library_series(db, view="missing", subscribed={(4321, 1)}) == ([], 0)
    assert [s["state"] for s in library_series(db, excluded={"4321"})[0]] == ["notmdb", "excluded"]
    db.execute("UPDATE tmdb_seasons SET episodes=? WHERE season=1", (json.dumps({str(e): "2020-01-01" for e in (1, 2, 4)}),))
    assert library_series(db, view="missing") == ([], 0)  # TMDB 上就這幾集：中間的空洞不算缺
    assert library_series(db)[0][1]["state"] == "complete"


class FakeMP:
    """MoviePilot V3：TMDB 集數（包在 data 裡）、建訂閱和搜尋都要登入。"""

    def __init__(self, episodes, existing=(), v2=False):
        self.episodes, self.existing, self.v2 = episodes, set(existing), v2
        self.sent = []
        self.subs = []  # MoviePilot 裡現有的訂閱

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        path = request.url.path
        if path == "/api/v1/login/access-token":
            return httpx.Response(200, json={"access_token": "jwt", "token_type": "bearer"})
        if path.startswith("/api/v1/tmdb/"):
            _, tmdbid, season = path.rsplit("/", 2)
            eps = self.episodes.get((int(tmdbid), int(season)))
            if eps is None:
                return httpx.Response(404, json={"detail": "Not Found"})
            items = [{"episode_number": e, "air_date": d} for e, d in eps]
            return httpx.Response(200, json=items if self.v2 else {"success": True, "data": items})
        if request.headers.get("authorization") != "Bearer jwt":
            return httpx.Response(401, json={"detail": "Not authenticated"})  # 建訂閱、搜尋不接受 API 令牌
        if path == "/api/v1/subscribe/" and request.method == "GET":  # 現有的訂閱，一次只給 2 個（分頁）
            return httpx.Response(200, json=self.subs[:2])
        if request.method == "DELETE" and path.startswith("/api/v1/subscribe/"):
            sid = int(path.rsplit("/", 1)[1])
            if sid == 3:
                return httpx.Response(500, json={"detail": "boom"})
            if not any(s["id"] == sid for s in self.subs):
                return httpx.Response(404, json={"detail": "订阅不存在"})
            self.subs = [s for s in self.subs if s["id"] != sid]
            return httpx.Response(200, json={"success": True, "data": {"status": "deleted"}})
        if path == "/api/v1/subscribe/":
            body = json.loads(request.content)
            if self.v2 and body["season"] in self.existing:
                return httpx.Response(200, json={"success": False, "message": "媒体库中已存在"})
            if body["season"] in self.existing:
                return httpx.Response(200, json={"success": True, "message": "订阅已存在", "data": {"id": 9}})
            return httpx.Response(200, json={"success": True, "message": "新增订阅成功", "data": {"id": 7}})
        if path.startswith("/api/v1/subscribe/search/"):
            if self.v2 and request.method == "POST":
                return httpx.Response(405, json={"detail": "Method Not Allowed"})
            return httpx.Response(200, json={"success": True, "message": "已安排搜索，很快开始"})
        return httpx.Response(404)

    def posts(self, path):
        return [json.loads(q.content) if q.content else None for q in self.sent
                if q.url.path == path and q.method != "GET" and q.headers.get("authorization")]


SHOW_A = {"id": 1, "name": "Show A", "year": 2020, "tmdbid": 4321, "seasons": [
    {"season": 1, "count": 3, "first": 1, "last": 4, "gaps": [3]},
    {"season": 2, "count": 1, "first": 1, "last": 1, "gaps": []},
]}
SHOW_B = {"id": 2, "name": "Show B", "year": None, "tmdbid": None,
          "seasons": [{"season": 1, "count": 1, "first": 1, "last": 1, "gaps": []}]}


def test_fill_subscribes_only_missing_seasons_and_searches(tmp_path: Path):
    fake = FakeMP({
        (4321, 1): [(1, "2020-01-01"), (2, "2020-01-02"), (3, "2020-01-03"), (4, "2020-01-04"), (5, "2020-01-05"),
                    (6, "2999-01-01")],  # 第 6 集還沒播
        (4321, 2): [(1, "2021-01-01")],
    })
    cfg = make_config(tmp_path, username="cat", password="pw")
    mp = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(fake))
    r = mp.fill([SHOW_A, SHOW_B], "manual")
    assert (r.total, r.done, r.created, r.complete, r.skipped, r.failed, r.missing) == (2, 2, 1, 1, 1, 0, 2)
    assert not r.errors
    # 只替缺集的第 1 季建訂閱，用 tmdbid 指定是哪一部；第 2 季已經齊全不建
    assert fake.posts("/api/v1/subscribe/") == [{
        "name": "Show A", "year": "2020", "type": "电视剧", "season": 1,
        "media_source": "themoviedb", "media_id": "4321", "tmdbid": 4321,
    }]
    assert [q.method for q in fake.sent if q.url.path == "/api/v1/subscribe/search/7"] == ["POST"]  # 馬上搜尋
    assert r.details == [
        "Show B：沒有 tmdbid，略過（先刮削）",
        "Show A S01：缺 2 集（E03、E05）；新增订阅成功；已安排搜索，很快开始",
        "Show A S02：TMDB 已播出的 1 集都有，不建訂閱",
    ]

    # 只有 API 令牌、沒有帳號密碼：說清楚要填什麼，不送
    only_token = MoviePilot(make_config(tmp_path).moviepilot, cfg, transport=httpx.MockTransport(fake))
    r = only_token.fill([SHOW_A], "manual")
    assert r.total == 0 and "帳號密碼" in r.errors[0]


def test_fill_does_not_search_existing_subscriptions_again(tmp_path: Path):
    fake = FakeMP({(4321, 1): [(e, "2020-01-01") for e in range(1, 5)]}, existing={1})
    cfg = make_config(tmp_path, username="cat", password="pw")
    mp = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(fake))
    r = mp.fill([{**SHOW_A, "seasons": SHOW_A["seasons"][:1]}], "manual")
    # V3 對已存在的訂閱回 success，不能算成新建；不再請它搜（它自己會定時搜，每週全量同步後重搜幾百個會被站點擋）
    assert (r.created, r.existing) == (0, 1)
    assert fake.posts("/api/v1/subscribe/search/9") == []
    assert r.details == ["Show A S01：缺 1 集（E03）；订阅已存在；之前就訂閱過，MoviePilot 會在定時搜尋時處理"]


def test_fill_spaces_out_new_subscriptions(tmp_path: Path):
    """每個新訂閱都會讓 MoviePilot 搜一遍所有站點：兩個新訂閱之間隔 fill_interval 秒，之前就訂閱過的不用等它。"""
    fake = FakeMP({(4321, s): [(e, "2020-01-01") for e in range(1, 9)] for s in (1, 2, 3)}, existing={2})
    cfg = make_config(tmp_path, username="cat", password="pw", fill_interval=60)
    mp = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(fake))
    now, waits = [1000.0], []

    class Clock:  # 假的時鐘：等多久就往前撥多久
        def wait(self, t):
            waits.append(round(t))
            now[0] += t
            return False

        def is_set(self):
            return False

    mp._clock, mp._stop = (lambda: now[0]), Clock()
    show = {**SHOW_A, "seasons": [{"season": s, "count": 1, "first": 1, "last": 1, "gaps": []} for s in (1, 2, 3)]}
    r = mp.fill([show], "manual")
    assert (r.created, r.existing, r.failed) == (2, 1, 0)
    assert sum(waits) == 60  # 第 1 季建好後等 60 秒；第 2 季之前就訂閱過，第 3 季不用再等
    assert len(fake.posts("/api/v1/subscribe/search/7")) == 2 and fake.posts("/api/v1/subscribe/search/9") == []


def test_fill_works_with_moviepilot_v2(tmp_path: Path):
    # V2：TMDB 集數直接是清單、搜尋只接受 GET、媒體庫齊全時拒絕建訂閱
    fake = FakeMP({(4321, 1): [(e, "") for e in range(1, 5)]}, existing={2}, v2=True)  # 沒日期但都在最後一集之前
    cfg = make_config(tmp_path, username="cat", password="pw", fill_interval=0)
    mp = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(fake))
    r = mp.fill([SHOW_A], "manual")
    # 沒有日期、但在媒體庫最後一集之前的算播過；第 2 季查不到 TMDB 集數，交給 MoviePilot 判斷
    assert (r.created, r.complete, r.failed) == (1, 1, 0) and not r.errors
    assert [q.method for q in fake.sent if q.url.path == "/api/v1/subscribe/search/7"] == ["POST", "GET"]
    assert r.details[1] == "Show A S02：查不到 TMDB 集數，交給 MoviePilot 判斷；媒体库中已存在"


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
    r = c.get("/web/api/series", params={"year": 2020}, headers=h).json()
    assert r["total"] == 1 and r["years"] == [2020]  # years 給下拉選單，不受篩選影響
    assert c.get("/web/api/series", params={"year": 1999}, headers=h).json() | {"years": None} == {
        "items": [], "total": 0, "offset": 0, "more": False, "years": None, "excluded": 0, "subscriptions": None,
        "stats": {"missing": 0, "subscribed": 0, "unchecked": 1, "notmdb": 1, "complete": 0, "excluded": 0, "total": 2}}
    assert c.get("/web/api/series", params={"view": "missing"}, headers=h).json()["total"] == 0  # 還沒對照過 TMDB
    assert c.get("/web/api/series", params={"view": "unchecked"}, headers=h).json()["total"] == 2

    calls = []
    app.state.moviepilot.fill_in_background = lambda shows, source: calls.append(([s["name"] for s in shows], source)) or True
    assert c.post("/web/api/moviepilot/fill", json={"series": [r["items"][0]["id"]]}, headers=h).json()["started"]
    assert c.post("/web/api/moviepilot/fill", json={}, headers=h).json()["started"]  # 全部：只送有 tmdbid 的
    assert calls == [(["Show A"], "manual"), (["Show A"], "manual")]
    status = c.get("/web/api/moviepilot/status", headers=h).json()
    assert status["can_subscribe"] is True and status["fill"]["running"] is False

    # 一季缺的集數超過「缺超過幾集的季不補」不建訂閱；標了「不補」的劇整部跳過（照 tmdbid 記在資料庫）
    fake = FakeMP({(4321, 1): [(e, "2020-01-01") for e in range(1, 9)], (4321, 2): [(1, "2021-01-01")]})
    mp = app.state.moviepilot
    mp._transport = httpx.MockTransport(fake)
    # 檢查缺集：只對照 TMDB、記下每一季缺哪幾集，不建訂閱；清單「只看缺集的」就列得出來
    f = mp.fill([SHOW_A], "manual", check=True)
    assert (f.check, f.total, f.lacking, f.missing, f.complete, f.created) == (True, 2, 1, 5, 1, 0)
    assert f.details == ["Show A S01：缺 5 集（E03、E05–E08）", "Show A S02：TMDB 已播出的 1 集都有"]
    assert not fake.posts("/api/v1/subscribe/")
    listed = c.get("/web/api/series", params={"view": "missing"}, headers=h).json()
    assert [s["name"] for s in listed["items"]] == ["Show A"] and (listed["stats"]["missing"], listed["stats"]["unchecked"]) == (1, 0)
    assert [(s["tmdb"], s["missing"], s["subscribed"]) for s in listed["items"][0]["seasons"]] == [(8, [3, 5, 6, 7, 8], False), (1, [], None)]
    assert listed["subscriptions"] == 0  # MoviePilot 現在有幾個訂閱
    # MoviePilot 裡已經訂閱了那一季：清單標已訂閱、換到「已訂閱」分頁，「補全缺集的」不再送它
    fake.subs = [{"id": 9, "name": "Show A", "type": "电视剧", "season": 1, "tmdbid": 4321}]
    mp._subs_cache = None
    listed = c.get("/web/api/series", params={"view": "subscribed"}, headers=h).json()
    assert [(s["name"], s["state"], s["seasons"][0]["subscribed"]) for s in listed["items"]] == [("Show A", "subscribed", True)]
    assert listed["subscriptions"] == 1 and listed["stats"]["missing"] == 0
    assert c.post("/web/api/moviepilot/fill", json={"view": "missing"}, headers=h).status_code == 400  # 沒有要補的
    fake.subs = []
    mp._subs_cache = None
    picked = []
    real_bg, mp.fill_in_background = mp.fill_in_background, lambda shows, source, check=False: picked.append([s["name"] for s in shows]) or True
    assert c.post("/web/api/moviepilot/fill", json={"view": "missing"}, headers=h).json()["count"] == 1 and picked == [["Show A"]]
    mp.fill_in_background = real_bg
    asked = len([q for q in fake.sent if "/tmdb/" in q.url.path])
    mp.cfg.fill_max_missing = 3
    f = mp.fill([SHOW_A], "manual")
    assert len([q for q in fake.sent if "/tmdb/" in q.url.path]) == asked == 2  # 剛查過的季用記下的，不再問 TMDB
    assert (f.total, f.done, f.created, f.too_many, f.complete, f.missing) == (2, 2, 0, 1, 1, 0)
    assert f.details[0] == "Show A S01：缺 5 集（E03、E05–E08），超過「缺超過幾集的季不補」的 3 集，不建訂閱"
    assert not fake.posts("/api/v1/subscribe/")
    r = c.post("/web/api/moviepilot/fill/exclude", json={"tmdbid": 4321, "name": "Show A", "exclude": True}, headers=h)
    assert r.json() == {"excluded": True, "count": 1}
    listed = c.get("/web/api/series", headers=h).json()
    # 標了「不補」的排在最後（清單照接下來要做什麼排）
    assert listed["excluded"] == 1 and [(s["name"], s["excluded"]) for s in listed["items"]] == [("Show B", False), ("Show A", True)]
    assert [s["name"] for s in c.get("/web/api/series", params={"excluded": 1}, headers=h).json()["items"]] == ["Show A"]
    f = mp.fill([SHOW_A, SHOW_B], "manual")
    assert (f.total, f.excluded, f.skipped) == (0, 1, 1) and "Show A：標了「不補」，略過" in f.details
    assert c.post("/web/api/moviepilot/fill/exclude", json={"tmdbid": 4321, "exclude": False}, headers=h).json()["count"] == 0
    assert c.post("/web/api/moviepilot/fill/exclude", json={"name": "Show B"}, headers=h).status_code == 400
    assert mp.fill([SHOW_A], "manual").total == 2

    # 沒填帳號密碼：不送，說清楚原因
    app2 = build(tmp_path / "nologin")
    c2 = TestClient(app2)
    token2 = c2.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]
    resp = c2.post("/web/api/moviepilot/fill", json={}, headers={"X-Emby-Token": token2})
    assert resp.status_code == 400 and "帳號" in resp.text
    # 只檢查不建訂閱，所以不用帳號密碼
    checks = []
    app2.state.moviepilot.fill_in_background = lambda shows, source, check=False: checks.append(check) or True
    assert c2.post("/web/api/moviepilot/fill", json={"check": True}, headers={"X-Emby-Token": token2}).json()["started"]
    assert checks == [True]
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


def test_undated_episodes_after_the_last_one_are_not_counted(tmp_path: Path):
    # TMDB 的佔位集：沒有播出日期、在媒體庫最後一集之後，不確定播了沒，不能叫 MoviePilot 去搜
    fake = FakeMP({
        (4321, 1): [(1, "2020-01-01"), (2, "2020-01-02"), (3, ""), (4, "2020-01-04"), (5, ""), (6, "")],
        (4321, 2): [(1, "2021-01-01"), (2, ""), (3, "")],
    })
    cfg = make_config(tmp_path, username="cat", password="pw")
    mp = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(fake))
    r = mp.fill([SHOW_A], "manual")
    assert (r.created, r.complete, r.missing) == (1, 1, 1)
    assert r.details == [
        # 第 3 集沒日期，但媒體庫已經有第 4 集，一定播過
        "Show A S01：缺 1 集（E03）；新增订阅成功；已安排搜索，很快开始；"
        "另有 2 集（E05–E06）TMDB 沒有播出日期，不確定播了沒，沒算進去",
        "Show A S02：TMDB 已播出的 1 集都有，不建訂閱；另有 2 集（E02–E03）TMDB 沒有播出日期，不確定播了沒，沒算進去",
    ]


def test_unsubscribe_all(tmp_path: Path):
    """取消 MoviePilot 裡所有的訂閱：要輸入確認字、要帳號登入；清單分頁也刪得完，刪不掉的記下來、其他照刪。"""
    app = build(tmp_path, username="cat", password="pw")
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    fake = FakeMP({})
    fake.subs = [{"id": i, "name": f"劇 {i}", "type": "电影" if i == 5 else "电视剧", "season": 1} for i in range(1, 6)]
    mp = app.state.moviepilot
    mp._transport = httpx.MockTransport(fake)
    assert c.get("/web/api/moviepilot/subscriptions", headers=h).json() == {"total": 2, "tv": 2, "other": 0}  # 假的一頁 2 個
    assert c.post("/web/api/moviepilot/subscriptions/clear", json={}, headers=h).status_code == 400
    assert len(fake.subs) == 5
    r = mp.unsubscribe_all()
    assert (r.total, r.done, r.failed, r.stopped) == (5, 4, 1, False) and "劇 3 S01" in r.errors[0]
    assert [s["id"] for s in fake.subs] == [3]  # 刪不掉的那個留著，不會一直重試
    assert c.get("/web/api/moviepilot/status", headers=h).json()["unsubscribe"]["done"] == 4
    # 經過 API：帶了確認字才開始（在背景跑）
    started = []
    mp.unsubscribe_all_in_background = lambda: started.append(1) or True
    assert c.post("/web/api/moviepilot/subscriptions/clear", json={"confirm": "取消訂閱"}, headers=h).json()["started"]
    assert started == [1]
    # 只有 API 令牌：訂閱的 API 要帳號登入
    c2 = TestClient(build(tmp_path / "nologin"))
    h2 = {"X-Emby-Token": c2.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    assert c2.get("/web/api/moviepilot/subscriptions", headers=h2).status_code == 400
    assert c2.post("/web/api/moviepilot/subscriptions/clear", json={"confirm": "取消訂閱"}, headers=h2).status_code == 400
