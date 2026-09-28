"""片頭片尾：從播放進度學，給播放器 Emby 章節標記和 Jellyfin Intro Skipper 格式。"""

from pathlib import Path

from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.intro import TICK

RUNTIME = 40 * 60 * TICK
EP_NFO = "<episodedetails><title>第 {n} 集</title><season>1</season><episode>{n}</episode><runtime>{runtime}</runtime></episodedetails>"


def build(tmp_path: Path, runtime: int = 40, **server):
    show = tmp_path / "tv" / "Show (2020)"
    show.mkdir(parents=True)
    for n in (1, 2, 3):
        (show / f"S01E0{n}.strm").write_text("http://x/a.mkv")
        (show / f"S01E0{n}.nfo").write_text(EP_NFO.format(n=n, runtime=runtime), encoding="utf-8")
    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data"), **server},
        "users": [{"name": "admin", "password": "pw", "admin": True}, {"name": "kid", "password": "pw"}],
        "libraries": [{"name": "劇集", "type": "tvshows", "paths": [str(tmp_path / "tv")]}],
    }), scan_on_start=False)
    app.state.scanner.scan_all()
    c = TestClient(app)
    return app, c


class Player:
    """模擬播放器：clock 是 Mi302 那邊的單調時鐘，我們自己往前撥。"""

    def __init__(self, app, c, user="admin"):
        self.app, self.c = app, c
        self.h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": user, "Pw": "pw"}).json()["AccessToken"]}
        self.now = 1000.0
        app.state.intro.clock = lambda: self.now

    def progress(self, item_id, seconds, after=10.0, stopped=False):
        self.now += after
        path = "/Sessions/Playing/Stopped" if stopped else "/Sessions/Playing/Progress"
        assert self.c.post(path, json={"ItemId": item_id, "PositionTicks": int(seconds * TICK)}, headers=self.h).status_code == 204


def episodes(c, h):
    series = c.get("/Items", params={"IncludeItemTypes": "Series", "Recursive": "true"}, headers=h).json()["Items"][0]
    return [e["Id"] for e in c.get(f"/Shows/{series['Id']}/Episodes", headers=h).json()["Items"]]


def markers(c, h, item_id):
    return {ch["MarkerType"]: ch["StartPositionTicks"] / TICK for ch in c.get(f"/Items/{item_id}", headers=h).json().get("Chapters", [])
            if ch["MarkerType"] != "Chapter"}


def test_learns_intro_from_a_skip_and_applies_to_the_season(tmp_path: Path):
    app, c = build(tmp_path)
    p = Player(app, c)
    e1, e2, e3 = episodes(c, p.h)
    assert markers(c, p.h, e1) == {}
    p.progress(e1, 0, after=0)
    p.progress(e1, 10)  # 正常播 10 秒
    p.progress(e1, 100)  # 10 秒內從 10 秒跳到 100 秒：片頭
    p.progress(e1, 110)
    assert markers(c, p.h, e1) == {"IntroStart": 10, "IntroEnd": 100}
    assert markers(c, p.h, e2) == {"IntroStart": 10, "IntroEnd": 100}  # 同一季其他集套用

    # 倍速播放不算跳：20 秒走了 40 秒
    p.progress(e2, 0, after=0)
    p.progress(e2, 40, after=20)
    assert markers(c, p.h, e2) == {"IntroStart": 10, "IntroEnd": 100}

    # 片尾：離結尾 90 秒時停下（切下一集）
    p.progress(e1, 2300)
    p.progress(e1, 2310, stopped=True)
    m = markers(c, p.h, e1)
    assert m["CreditsStart"] == 2310
    assert markers(c, p.h, e3)["CreditsStart"] == 2310  # 同樣片長，按「距離結尾」套用
    # 播完了不算片尾
    p.progress(e3, 2390, after=0)
    p.progress(e3, 2395, stopped=True)
    assert markers(c, p.h, e3)["CreditsStart"] == 2310

    # Jellyfin Intro Skipper 和 MediaSegments 的格式
    ts = c.get(f"/Episode/{e2}/IntroTimestamps", headers=p.h).json()
    assert (ts["Valid"], ts["IntroStart"], ts["IntroEnd"], ts["HideSkipPromptAt"]) == (True, 10.0, 100.0, 20.0)
    both = c.get(f"/Episode/{e2}/Timestamps", headers=p.h).json()
    assert both["Credits"]["IntroStart"] == 2310.0 and both["Credits"]["IntroEnd"] == 2400.0
    seg = c.get(f"/MediaSegments/{e2}", headers=p.h).json()["Items"]
    assert [(s["Type"], s["StartTicks"] / TICK) for s in seg] == [("Intro", 10), ("Outro", 2310)]
    assert "Chapters" in c.get("/Items", params={"Ids": e2, "Fields": "Chapters"}, headers=p.h).json()["Items"][0]

    st = c.get("/web/api/intro/status", headers=p.h).json()
    assert (st["seasons"], st["episodes"]) == (1, 1)
    season = c.get("/web/api/intro/seasons", headers=p.h).json()["items"][0]
    assert season["intro"] == [10, 100] and season["credits_tail"] == 90
    assert c.post("/web/api/intro/clear", json={}, headers=p.h).json()["removed"] == 2
    assert markers(c, p.h, e2) == {} and c.get(f"/Episode/{e2}/IntroTimestamps", headers=p.h).status_code == 404


def test_median_across_users_and_own_marks_win(tmp_path: Path):
    app, c = build(tmp_path)
    a, b = Player(app, c, "admin"), Player(app, c, "kid")
    e1, e2, e3 = episodes(c, a.h)
    for player, end in ((a, 90), (b, 110)):
        player.progress(e1, 0, after=0)
        player.progress(e1, 5)
        player.progress(e1, end)
    assert markers(c, a.h, e1)["IntroEnd"] == 100  # 兩個人取中位數
    a.progress(e2, 0, after=0)
    a.progress(e2, 30)
    a.progress(e2, 150)
    assert markers(c, a.h, e2) == {"IntroStart": 30, "IntroEnd": 150}  # 自己有紀錄的用自己的
    assert markers(c, a.h, e3)["IntroEnd"] == 110  # 沒紀錄的用整季中位數（90、110、150）


def test_disabled_learns_and_serves_nothing(tmp_path: Path):
    app, c = build(tmp_path, intro_skip=False)
    p = Player(app, c)
    e1, *_ = episodes(c, p.h)
    p.progress(e1, 0, after=0)
    p.progress(e1, 5)
    p.progress(e1, 100)
    assert markers(c, p.h, e1) == {} and app.state.db.one("SELECT COUNT(*) AS c FROM intro_obs")["c"] == 0
    assert c.get(f"/Episode/{e1}/IntroTimestamps", headers=p.h).status_code == 404


def test_learns_credits_from_a_jump(tmp_path: Path):
    app, c = build(tmp_path)
    p = Player(app, c)
    e1, e2, e3 = episodes(c, p.h)
    p.progress(e1, 2174, after=0)
    p.progress(e1, 2184)  # 36:24，最後 5 分鐘裡
    p.progress(e1, 2395)  # 10 秒內跳到 39:55：跳過片尾
    assert markers(c, p.h, e1) == {"CreditsStart": 2184}
    assert markers(c, p.h, e2) == {"CreditsStart": 2184}  # 同一季套用
    # 片尾區裡往前跳 60 秒以上也算（ED 後面還有預告，不一定跳到結尾）
    p.progress(e2, 2150, after=0)
    p.progress(e2, 2160)
    p.progress(e2, 2250)
    assert markers(c, p.h, e2) == {"CreditsStart": 2160}
    # 片尾區裡只跳一小段、沒到結尾：不算；還沒到片尾區（34:10）就跳到結尾：也不算
    p.progress(e3, 2160, after=0)
    p.progress(e3, 2170)
    p.progress(e3, 2200)
    p.progress(e3, 2040, after=0)
    p.progress(e3, 2050)
    p.progress(e3, 2395)
    assert markers(c, p.h, e3) == {"CreditsStart": 2172}  # 沒有自己的紀錄，整季中位數


def test_intro_window_and_jump_length(tmp_path: Path):
    app, c = build(tmp_path)
    p = Player(app, c)
    e1, e2, e3 = episodes(c, p.h)
    # 冷開場之後才進片頭：9:39 跳到 10:28
    p.progress(e1, 569, after=0)
    p.progress(e1, 579)
    p.progress(e1, 628)
    assert markers(c, p.h, e1) == {"IntroStart": 579, "IntroEnd": 628}
    # 一次跳超過 3 分鐘是跳過劇情，不是片頭
    p.progress(e2, 0, after=0)
    p.progress(e2, 10)
    p.progress(e2, 300)
    assert markers(c, p.h, e2) == {"IntroStart": 579, "IntroEnd": 628}  # 只有整季套用的
    # 10 分鐘以後才跳的不算
    p.progress(e3, 700, after=0)
    p.progress(e3, 710)
    p.progress(e3, 800)
    assert markers(c, p.h, e3) == {"IntroStart": 579, "IntroEnd": 628}


def test_short_episodes_use_a_quarter(tmp_path: Path):
    app, c = build(tmp_path, runtime=24)  # 動畫：片頭區前 6 分鐘、片尾區最後 5 分鐘
    p = Player(app, c)
    e1, e2, _ = episodes(c, p.h)
    p.progress(e1, 380, after=0)
    p.progress(e1, 390)  # 6:30 才跳：不算
    p.progress(e1, 480)
    assert markers(c, p.h, e1) == {}
    p.progress(e2, 320, after=0)
    p.progress(e2, 330)  # 5:30 跳：算
    p.progress(e2, 420)
    assert markers(c, p.h, e2) == {"IntroStart": 330, "IntroEnd": 420}
    p.progress(e2, 1100, after=0)
    p.progress(e2, 1110, stopped=True)  # 18:30 停：還沒到最後 5 分鐘
    assert "CreditsStart" not in markers(c, p.h, e2)
    p.progress(e2, 1300, after=0)
    p.progress(e2, 1310, stopped=True)  # 21:50 停：片尾
    assert markers(c, p.h, e2)["CreditsStart"] == 1310


def test_manual_settings_override_learning_and_search(tmp_path: Path):
    app, c = build(tmp_path)
    show = tmp_path / "tv" / "Show (2020)"
    for n in (1, 2):
        (show / "Season 2").mkdir(exist_ok=True)
        (show / "Season 2" / f"S02E0{n}.strm").write_text("http://x/b.mkv")
        (show / "Season 2" / f"S02E0{n}.nfo").write_text(
            f"<episodedetails><season>2</season><episode>{n}</episode><runtime>40</runtime></episodedetails>", encoding="utf-8")
    other = tmp_path / "tv" / "庆余年 (2019)"
    other.mkdir()
    (other / "S01E01.strm").write_text("http://x/c.mkv")
    app.state.scanner.scan_all()
    db = app.state.db
    series = db.one("SELECT id FROM items WHERE type='Series' AND name='Show'")["id"]
    ep = {(r["parent_index_number"], r["index_number"]): str(r["id"]) for r in db.query(
        "SELECT id, parent_index_number, index_number FROM items WHERE type='Episode' AND series_id=?", (series,))}
    s1, s2 = (db.one("SELECT id FROM items WHERE type='Season' AND series_id=? AND index_number=?", (series, n))["id"] for n in (1, 2))
    p = Player(app, c)
    p.progress(ep[1, 1], 0, after=0)
    p.progress(ep[1, 1], 10)
    p.progress(ep[1, 1], 100)  # 學到第 1 季片頭 10–100 秒

    def seasons(**params):
        return c.get("/web/api/intro/seasons", params=params, headers=p.h).json()

    def put(season_id, body):
        return c.put(f"/web/api/intro/seasons/{season_id}", json=body, headers=p.h)

    r = seasons()
    assert r["total"] == 1 and r["items"][0]["season_id"] == s1
    assert (r["items"][0]["intro"], r["items"][0]["manual"], r["items"][0]["learned"]) == ([10, 100], None, 1)
    # 搜尋：沒學到的季也列出來，拼音首字母也認
    assert [(i["series"], i["season"], i["intro"]) for i in seasons(q="qyn")["items"]] == [("庆余年", 1, None)]
    assert [i["season"] for i in seasons(q="show")["items"]] == [1, 2]

    # 手動設定：片頭 5–80 秒、片尾在結尾前 60 秒；之後學到的不再影響
    assert put(s1, {"intro": {"mode": "manual", "start": 5, "end": 80}, "credits": {"mode": "manual", "tail": 60}}).json() == {"updated": 1}
    assert markers(c, p.h, ep[1, 2]) == {"IntroStart": 5, "IntroEnd": 80, "CreditsStart": 2340}
    ts = c.get(f"/Episode/{ep[1, 2]}/IntroTimestamps", headers=p.h).json()
    assert (ts["IntroStart"], ts["IntroEnd"]) == (5.0, 80.0)
    p.progress(ep[1, 2], 0, after=0)
    p.progress(ep[1, 2], 20)
    p.progress(ep[1, 2], 150)
    assert markers(c, p.h, ep[1, 2])["IntroEnd"] == 80
    item = seasons()["items"][0]
    assert (item["intro"], item["credits_tail"], item["auto"]["intro"]) == ([5, 80], 60, [10, 100])  # 清單用第 1 集算
    assert item["manual"] == {"intro_mode": "manual", "intro": [5, 80], "credits_mode": "manual", "credits_tail": 60}
    assert c.get("/web/api/intro/status", headers=p.h).json()["manual"] == 1

    # 這一季沒有片頭：不給跳過片頭；片尾改回照學的（還沒學到）
    put(s1, {"intro": {"mode": "none"}, "credits": {"mode": "auto"}})
    assert markers(c, p.h, ep[1, 1]) == {}
    assert c.get(f"/Episode/{ep[1, 1]}/IntroTimestamps", headers=p.h).status_code == 404

    # 同一部劇的每一季都用同一個設定
    assert put(s1, {"intro": {"mode": "manual", "start": 3, "end": 60}, "all_seasons": True}).json() == {"updated": 2}
    assert markers(c, p.h, ep[2, 1]) == {"IntroStart": 3, "IntroEnd": 60}

    # 清除學到的紀錄不動手動設定；兩個都改回自動就回到學的值
    c.post("/web/api/intro/clear", json={}, headers=p.h)
    assert markers(c, p.h, ep[1, 1])["IntroEnd"] == 60
    p.progress(ep[1, 1], 0, after=0)
    p.progress(ep[1, 1], 10)
    p.progress(ep[1, 1], 100)
    put(s1, {"intro": {"mode": "auto"}, "credits": {"mode": "auto"}})
    assert markers(c, p.h, ep[1, 1]) == {"IntroStart": 10, "IntroEnd": 100}
    assert db.one("SELECT COUNT(*) AS c FROM intro_manual WHERE season_id=?", (s1,))["c"] == 0

    # 不合理的值
    assert put(s1, {"intro": {"mode": "manual", "start": 90, "end": 30}}).status_code == 400
    assert put(s1, {"credits": {"mode": "manual", "tail": 0}}).status_code == 400
    assert put(s1, {"intro": {"mode": "maybe"}}).status_code == 400
    assert put(s1, {"intro": {"mode": "manual", "start": "a", "end": 3}}).status_code == 400
    assert put(999999, {"intro": {"mode": "none"}}).status_code == 404

    # 季從媒體庫消失：手動設定跟著清掉
    import shutil
    shutil.rmtree(show / "Season 2")
    app.state.scanner.scan_all()
    assert db.one("SELECT COUNT(*) AS c FROM intro_manual WHERE season_id=?", (s2,))["c"] == 0


def test_season_list_stays_fast_on_a_big_library(tmp_path: Path):
    """幾萬集的媒體庫：片頭片尾的季清單不能對每一季逐一查（以前 4 萬多集要十幾秒，期間整個資料庫被佔住，網頁白屏）。"""
    import time
    import types

    from embyserver.db import Database
    from embyserver.intro import IntroLearner

    db = Database(str(tmp_path / "big.db"))
    rows, nid = [], 1
    for s in range(1200):
        series = nid
        rows.append((series, "Series", f"劇{s}", None, None, None))
        nid += 1
        for season in (1, 2):
            season_id = nid
            rows.append((season_id, "Season", f"第 {season} 季", series, None, season))
            nid += 1
            for e in range(1, 13):
                rows.append((nid, "Episode", f"第 {e} 集", series, season_id, e))
                nid += 1
    with db.lock:
        db.conn.executemany(
            "INSERT INTO items(id, type, name, path, series_id, season_id, index_number) VALUES(?,?,?,?,?,?,?)",
            [(i, t, n, f"/tv/{i}", ser if t != "Series" else None, sea, idx) for i, t, n, ser, sea, idx in rows])
        db.conn.execute("INSERT INTO intro_obs VALUES(?,?,?,?,?,?)", (nid - 1, "u", "intro", 10 * TICK, 90 * TICK, 1))
        db.conn.commit()
    intro = IntroLearner(db, types.SimpleNamespace(server=types.SimpleNamespace(intro_skip=True)))
    started = time.perf_counter()
    r = intro.seasons()
    assert time.perf_counter() - started < 1.0
    assert r["total"] == 1 and r["items"][0]["learned"] == 1
    plan = " ".join(row[3] for row in db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM items WHERE season_id=? AND type='Episode' ORDER BY index_number LIMIT 1", (3,)))
    assert "idx_items_season" in plan
