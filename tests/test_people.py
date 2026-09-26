"""演職人員：從 nfo 讀進來給播放器、人物頁、搜尋；中文名（MoviePilot → Wikidata）；類型中文化。"""

import json
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.people import PersonNames, localize_genres, pick_chinese

TVSHOW = """<tvshow><title>庆余年</title><genre>Drama</genre><genre>Sci-Fi &amp; Fantasy</genre><genre>劇情</genre>
<director tmdbid="1001">Sun Hao</director><credits tmdbid="1002">Wang Juan</credits>
<actor><name>Zhang Ruoyun</name><role>Fan Xian</role><type>Actor</type><tmdbid>1397017</tmdbid>
<thumb>https://image.tmdb.org/t/p/h632/a.jpg</thumb></actor>
<actor><name>Chen Daoming</name><role>Emperor</role><type>Actor</type><tmdbid>1397018</tmdbid></actor>
<actor><name>Someone Local</name><role>Extra</role><type>Actor</type><tmdbid></tmdbid></actor></tvshow>"""
EPISODE = """<episodedetails><title>第 1 集</title><season>1</season><episode>1</episode>
<actor><name>Guest Star</name><role>Cameo</role><type>GuestStar</type><tmdbid>2001</tmdbid></actor></episodedetails>"""
MOVIE = """<movie><title>流浪地球</title><genre>Science Fiction</genre><genre>Adventure</genre>
<actor><name>Wu Jing</name><role>Liu Peiqiang</role><type>Actor</type><tmdbid>1397017</tmdbid></actor></movie>"""


def build(tmp_path: Path, **server):
    show = tmp_path / "tv" / "庆余年 (2019)"
    show.mkdir(parents=True)
    (show / "tvshow.nfo").write_text(TVSHOW, encoding="utf-8")
    (show / "S01E01.strm").write_text("http://x/a.mkv")
    (show / "S01E01.nfo").write_text(EPISODE, encoding="utf-8")
    (show / "S01E02.strm").write_text("http://x/b.mkv")
    movie = tmp_path / "movies" / "流浪地球 (2019)"
    movie.mkdir(parents=True)
    (movie / "流浪地球 (2019).strm").write_text("http://x/c.mkv")
    (movie / "流浪地球 (2019).nfo").write_text(MOVIE, encoding="utf-8")
    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data"), **server},
        "users": [{"name": "admin", "password": "pw", "admin": True}],
        "libraries": [
            {"name": "劇集", "type": "tvshows", "paths": [str(tmp_path / "tv")]},
            {"name": "電影", "type": "movies", "paths": [str(tmp_path / "movies")]},
        ],
    }), scan_on_start=False)
    app.state.scanner.scan_all()
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    return app, c, h


def test_people_from_nfo_and_person_pages(tmp_path: Path):
    app, c, h = build(tmp_path)
    series = c.get("/Items", params={"IncludeItemTypes": "Series", "Recursive": "true"}, headers=h).json()["Items"][0]
    full = c.get(f"/Items/{series['Id']}", headers=h).json()
    assert [(p["Name"], p["Type"], p.get("Role")) for p in full["People"]] == [
        ("Sun Hao", "Director", None), ("Wang Juan", "Writer", None),
        ("Zhang Ruoyun", "Actor", "Fan Xian"), ("Chen Daoming", "Actor", "Emperor"), ("Someone Local", "Actor", "Extra"),
    ]
    zry = full["People"][2]
    assert zry["Id"] == "p1397017" and zry["PrimaryImageTag"] and "PrimaryImageTag" not in full["People"][3]
    assert full["People"][4]["Id"].startswith("pn")  # 沒有 tmdbid 用名稱雜湊
    assert full["Genres"] == ["剧情", "科幻奇幻"]  # 類型中文化，繁體轉簡體後去重

    eps = c.get(f"/Shows/{series['Id']}/Episodes", headers=h).json()["Items"]
    ep1 = c.get(f"/Items/{eps[0]['Id']}", headers=h).json()
    assert [(p["Name"], p["Type"]) for p in ep1["People"]] == [("Guest Star", "GuestStar")]
    ep2 = c.get(f"/Items/{eps[1]['Id']}", headers=h).json()
    assert ep2["People"][2]["Name"] == "Zhang Ruoyun"  # 集沒有自己的演員時用劇的
    assert "People" not in c.get("/Items", params={"IncludeItemTypes": "Series", "Recursive": "true"}, headers=h).json()["Items"][0]

    # 人物頁、頭像、作品
    person = c.get("/Items/p1397017", headers=h).json()
    assert person["Type"] == "Person" and person["ProviderIds"] == {"Tmdb": "1397017"} and person["ImageTags"]["Primary"]
    r = c.get("/Items/p1397017/Images/Primary", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://image.tmdb.org/t/p/h632/a.jpg"
    assert c.get("/Items/p1397018/Images/Primary").status_code == 404
    works = c.get("/Items", params={"PersonIds": "p1397017", "Recursive": "true", "IncludeItemTypes": "Movie,Series"}, headers=h).json()
    assert sorted(i["Name"] for i in works["Items"]) == ["庆余年", "流浪地球"]  # 同一個 tmdbid，兩部都算他的
    assert c.get("/Items", params={"Person": "Chen Daoming", "Recursive": "true"}, headers=h).json()["TotalRecordCount"] == 1
    assert c.get("/Persons/Wu Jing", headers=h).json()["Id"] == "p1397017"
    assert c.get("/Items/p999", headers=h).status_code == 404

    # 搜尋人：/Persons，或 IncludeItemTypes 帶 Person
    assert [p["Name"] for p in c.get("/Persons", params={"SearchTerm": "chen"}, headers=h).json()["Items"]] == ["Chen Daoming"]
    mixed = c.get("/Items", params={"SearchTerm": "Zhang", "IncludeItemTypes": "Series,Person", "Recursive": "true"}, headers=h).json()
    assert [i["Type"] for i in mixed["Items"]] == ["Person"] and mixed["TotalRecordCount"] == 1


def test_pick_chinese_and_genres():
    assert pick_chinese(["Michael Chen", "Chen He", "陈赫"], "Chen He") == "陈赫"
    assert pick_chinese(["陳道明"], "Chen Daoming") == "陈道明"  # 只有繁體就轉簡體
    assert pick_chinese(["陳道明", "陈道明"]) == "陈道明"  # 有簡體優先簡體
    assert pick_chinese(["チェン・ハー", "Chen He"]) is None  # 日文假名不算
    assert pick_chinese([], "陈赫") == "陈赫"  # 本來就是中文名
    # 英文換中文、繁體換簡體、去重（劇情/Drama 只留一個）、不認得的照原樣
    assert localize_genres(["Action", "Sci-Fi & Fantasy", "劇情", "Drama", "Unknown Genre", ""]) == [
        "动作", "科幻奇幻", "剧情", "Unknown Genre",
    ]


class FakeSources:
    """假的 MoviePilot 人物介面和 Wikidata。"""

    def __init__(self, mp: dict, wd: dict, mp_down=False, wd_down=False, mp_reject=()):
        self.mp, self.wd, self.mp_down, self.wd_down = mp, wd, mp_down, wd_down
        self.mp_reject = set(mp_reject)
        self.mp_calls, self.wd_queries = [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "query.wikidata.org":
            if self.wd_down:
                return httpx.Response(503, text="busy")
            query = httpx.QueryParams(request.content.decode())["query"]
            self.wd_queries.append(query)
            rows = []
            for tmdb, labels in self.wd.items():
                if f'"{tmdb}"' in query:
                    for lang, label in labels:
                        rows.append({"tmdb": {"value": tmdb}, "lang": {"value": lang}, "label": {"value": label}})
            return httpx.Response(200, json={"results": {"bindings": rows}})
        if request.url.path.startswith("/api/v1/tmdb/person/"):
            if self.mp_down:
                return httpx.Response(502, text="bad gateway")
            pid = request.url.path.rsplit("/", 1)[1]
            self.mp_calls.append(pid)
            if not pid.isdigit() or pid in self.mp_reject:  # MoviePilot V3 的 person_id: int
                return httpx.Response(422, json={"detail": [{"type": "int_parsing", "loc": ["path", "person_id"]}]})
            data = self.mp.get(pid)
            return httpx.Response(200, json={"success": True, "data": data} if data else {"success": False, "message": "无"})
        return httpx.Response(404)


def names_for(tmp_path, fake, mp=True):
    from embyserver.moviepilot import MoviePilot

    app, c, h = build(tmp_path)
    cfg = app.state.config
    if mp:
        cfg.moviepilot.url, cfg.moviepilot.api_token = "http://mp:3000", "tok"
    pilot = MoviePilot(cfg.moviepilot, cfg, transport=httpx.MockTransport(fake))
    pn = PersonNames(app.state.db, cfg, pilot, transport=httpx.MockTransport(fake))
    pn.pause = 0
    return app, c, h, pn


def test_chinese_names_from_moviepilot_then_wikidata(tmp_path: Path):
    fake = FakeSources(
        mp={"1397017": {"name": "Zhang Ruoyun", "also_known_as": ["張若昀", "张若昀"]}, "1001": {"name": "Sun Hao", "also_known_as": []}},
        wd={"1001": [("zh-hant", "孫皓"), ("zh-hans", "孙皓")], "2001": []},
    )
    app, c, h, pn = names_for(tmp_path, fake)
    assert sorted(pn.pending()) == ["1001", "1002", "1397017", "1397018", "2001"]
    assert pn.run() == 5
    rows = {r["tmdbid"]: (r["zh"], r["source"]) for r in app.state.db.query("SELECT * FROM person_names")}
    assert rows["1397017"] == ("张若昀", "moviepilot")  # TMDB 別名裡挑簡體
    assert rows["1001"] == ("孙皓", "wikidata")  # MoviePilot 沒有 → Wikidata（zh-hans 優先）
    assert rows["1002"] == (None, "none") and rows["2001"] == (None, "none")  # 兩邊都沒有
    assert pn.pending() == []  # 查過沒有的 30 天內不再查
    app.state.db.execute("UPDATE person_names SET at=? WHERE tmdbid='1002'", (int(time.time()) - 31 * 86400,))
    assert pn.pending() == ["1002"]
    assert len(fake.wd_queries) == 1  # 一批問完

    # 顯示中文名、搜中文名，原名還在
    series = c.get("/Items", params={"IncludeItemTypes": "Series", "Recursive": "true"}, headers=h).json()["Items"][0]
    people = c.get(f"/Items/{series['Id']}", headers=h).json()["People"]
    assert people[0]["Name"] == "孙皓" and people[2]["Name"] == "张若昀" and people[3]["Name"] == "Chen Daoming"
    person = c.get("/Items/p1397017", headers=h).json()
    assert (person["Name"], person["OriginalTitle"]) == ("张若昀", "Zhang Ruoyun")
    assert c.get("/Persons", params={"SearchTerm": "若昀"}, headers=h).json()["Items"][0]["Id"] == "p1397017"
    assert c.get("/Persons/张若昀", headers=h).json()["Id"] == "p1397017"
    app.state.config.server.chinese_people = False  # 關掉就顯示原名
    assert c.get("/Items/p1397017", headers=h).json()["Name"] == "Zhang Ruoyun"


def test_sources_down_are_retried_later(tmp_path: Path):
    fake = FakeSources(mp={}, wd={}, mp_down=True, wd_down=True)
    app, c, h, pn = names_for(tmp_path, fake)
    assert pn.run() == 0 and "MoviePilot" in pn.last_error or "Wikidata" in pn.last_error
    assert len(pn.pending()) == 5  # 沒問到的下次再查，不記成「沒有」

    fake.mp_down, fake.wd_down = False, True  # 只有 Wikidata 連不上：MoviePilot 查到的先寫
    fake.mp["1397018"] = {"name": "Chen Daoming", "also_known_as": ["陈道明"]}
    assert pn.run() == 1
    assert app.state.db.one("SELECT zh FROM person_names WHERE tmdbid='1397018'")["zh"] == "陈道明"
    assert len(pn.pending()) == 4

    # 沒設定 MoviePilot：只問 Wikidata，沒有就記「沒有」
    fake2 = FakeSources(mp={}, wd={"1001": [("zh", "孙皓")]})
    app2, c2, h2, pn2 = names_for(tmp_path / "b", fake2, mp=False)
    assert pn2.run() == 5 and fake2.mp_calls == []
    assert app2.state.db.one("SELECT zh, source FROM person_names WHERE tmdbid='1001'")["source"] == "wikidata"


def test_ids_moviepilot_cannot_look_up_do_not_block_the_batch(tmp_path: Path):
    fake = FakeSources(mp={"1397018": {"name": "Chen Daoming", "also_known_as": ["陈道明"]}}, wd={}, mp_reject={"1001"})
    app, c, h, pn = names_for(tmp_path, fake)
    db = app.state.db
    item_id = db.one("SELECT item_id FROM people LIMIT 1")["item_id"]
    db.execute("INSERT INTO people(item_id, ord, name, role, type, tmdbid, pid) VALUES(?,?,?,?,?,?,?)",
               (item_id, 9, "Imdb Person", "", "Director", "nm0000123", "pn-imdb"))
    assert "nm0000123" not in pn.pending()  # IMDb 的 id 不查
    assert pn.run() == 5 and pn.last_error == ""
    assert sorted(fake.mp_calls) == ["1001", "1002", "1397017", "1397018", "2001"]  # 被拒的那位之後照樣問
    assert db.one("SELECT source FROM person_names WHERE tmdbid='1001'")["source"] == "none"
    assert db.one("SELECT zh FROM person_names WHERE tmdbid='1397018'")["zh"] == "陈道明"
    assert pn.pending() == []  # 下次不會再卡在同一個人
    assert pn._from_wikidata(["nm0000123"]) == {} and len(fake.wd_queries) == 1  # 沒有數字 id 就不問 Wikidata


def test_people_status_and_resolve_endpoints(tmp_path: Path):
    app, c, h = build(tmp_path)
    app.state.person_names.run_in_background = lambda: True
    s = c.get("/web/api/people/status", headers=h).json()
    assert (s["people"], s["pending"], s["resolved"], s["chinese_people"]) == (6, 5, 0, True)
    assert c.post("/web/api/people/resolve", headers=h).json()["started"]
    app.state.config.server.chinese_people = False
    assert c.post("/web/api/people/resolve", headers=h).status_code == 400


def test_genres_localization_can_be_turned_off(tmp_path: Path):
    app, c, h = build(tmp_path, chinese_genres=False)
    series = c.get("/Items", params={"IncludeItemTypes": "Series", "Recursive": "true"}, headers=h).json()["Items"][0]
    assert series["Genres"] == ["Drama", "Sci-Fi & Fantasy", "劇情"]


def test_person_page_with_accented_name(tmp_path: Path):
    """路徑轉小寫只轉英文字母：É 保持原樣，SQLite 的 lower() 才對得上。"""
    app, c, h = build(tmp_path)
    show = tmp_path / "tv" / "庆余年 (2019)"
    (show / "tvshow.nfo").write_text(
        TVSHOW.replace("Chen Daoming", "Émilie Dequenne"), encoding="utf-8")
    app.state.scanner.scan_all()
    assert c.get("/Persons/Émilie Dequenne", headers=h).status_code == 200
