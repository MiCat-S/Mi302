"""列表端點先一次查好整頁的使用者資料、劇和季、子項數（dto.Prefetch）：輸出和逐項查一模一樣，查詢次數少很多。"""

from pathlib import Path

from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.routes import items as item_routes

SERIES, EPISODES = 400, 100  # 400 部劇、每部一季 100 集：四萬多筆


def build(tmp_path: Path):
    cfg = config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")},
        "users": [{"name": "cat", "password": "pw", "admin": True}],
        "libraries": [{"name": "劇集", "type": "tvshows", "paths": [str(tmp_path / "tv")]}],
    })
    app = create_app(cfg, scan_on_start=False)
    db = app.state.db
    lib = db.execute("INSERT INTO items(type, collection_type, name, sort_name, path) "
                     "VALUES('CollectionFolder', 'tvshows', '劇集', '劇集', 'library://劇集')").lastrowid
    db.execute("UPDATE items SET library_id=? WHERE id=?", (lib, lib))
    rows = []
    for s in range(SERIES):
        rows.append((lib, lib, "Series", f"劇 {s:03d}", f"ju {s:03d}", f"/tv/{s}", None, None, None, None, s % 30 + 1990))
    db.executemany("INSERT INTO items(library_id, parent_id, type, name, sort_name, path, series_id, season_id, "
                   "index_number, parent_index_number, year) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    series = [r["id"] for r in db.query("SELECT id FROM items WHERE type='Series' ORDER BY id")]
    db.executemany("INSERT INTO items(library_id, parent_id, type, name, sort_name, path, series_id, index_number) "
                   "VALUES(?,?,?,?,?,?,?,?)", [(lib, sid, "Season", "第 1 季", "0001", f"/tv/{sid}#season1", sid, 1)
                                               for sid in series])
    seasons = {r["series_id"]: r["id"] for r in db.query("SELECT id, series_id FROM items WHERE type='Season'")}
    db.executemany(
        "INSERT INTO items(library_id, parent_id, type, name, sort_name, path, is_strm, series_id, season_id, "
        "index_number, parent_index_number, runtime_ticks) VALUES(?,?,?,?,?,?,1,?,?,?,1,?)",
        [(lib, seasons[sid], "Episode", f"第 {e} 集", f"0001-{e:05d}", f"/tv/{sid}/S01E{e:03d}.strm", sid, seasons[sid], e,
          24000000000) for sid in series for e in range(1, EPISODES + 1)])
    c = TestClient(app)
    token = c.post("/Users/AuthenticateByName", json={"Username": "cat", "Pw": "pw"}).json()
    h, uid = {"X-Emby-Token": token["AccessToken"]}, token["User"]["Id"]
    # 看過、看一半、收藏：讓使用者資料和「沒看過幾集」有東西可比
    eps = [r["id"] for r in db.query("SELECT id FROM items WHERE type='Episode' ORDER BY id LIMIT 300")]
    db.executemany("INSERT INTO user_data(user_id, item_id, played, play_count, position_ticks, is_favorite, last_played) "
                   "VALUES(?,?,?,?,?,?,?)", [(uid, e, int(i % 3 == 0), i % 3, 0 if i % 3 else 1000, int(i % 7 == 0),
                                              "2026-09-01T00:00:00.0000000Z") for i, e in enumerate(eps)])
    return app, c, h, uid, lib, series


def count_queries(db):
    counter = {"n": 0}
    for name in ("query", "one"):
        orig = getattr(db, name)

        def wrapped(*a, _orig=orig, **kw):
            counter["n"] += 1
            return _orig(*a, **kw)
        setattr(db, name, wrapped)
    return counter


def test_list_endpoints_prefetch_same_output_fewer_queries(tmp_path: Path, monkeypatch):
    app, c, h, uid, lib, series = build(tmp_path)
    assert app.state.db.scalar("SELECT COUNT(*) FROM items") > 40000
    requests = [
        (f"/Users/{uid}/Items", {"ParentId": lib, "Recursive": "true", "IncludeItemTypes": "Episode", "Limit": 100}),
        (f"/Users/{uid}/Items", {"ParentId": lib, "Recursive": "true", "IncludeItemTypes": "Series", "Limit": 100,
                                 "SortBy": "SortName"}),
        (f"/Users/{uid}/Items", {"ParentId": series[0]}),
        (f"/Users/{uid}/Items", {"Recursive": "true", "Filters": "IsResumable,IsFavorite"}),
        (f"/Shows/{series[1]}/Episodes", {}),
        (f"/Shows/{series[1]}/Seasons", {}),
        (f"/Users/{uid}/Views", {}),
        (f"/Users/{uid}/Items/Latest", {"Limit": 50}),
        (f"/Users/{uid}/Items/Resume", {}),
        ("/Shows/NextUp", {}),
    ]
    counter = count_queries(app.state.db)
    fast, fast_n = [], []
    for path, params in requests:
        counter["n"] = 0
        fast.append(c.get(path, params=params, headers=h).json())
        fast_n.append(counter["n"])
    # 換回逐項查（沒有 prefetch）
    monkeypatch.setattr(item_routes, "_dtos", lambda request, ctx, rows, full=False:
                        [item_routes._dto(request, ctx, r, full) for r in rows])
    slow, slow_n = [], []
    for path, params in requests:
        counter["n"] = 0
        slow.append(c.get(path, params=params, headers=h).json())
        slow_n.append(counter["n"])
    assert fast == slow  # 輸出一模一樣
    episodes_page = fast[0]["Items"]
    assert len(episodes_page) == 100 and fast[0]["TotalRecordCount"] == SERIES * EPISODES
    assert any(i["UserData"]["Played"] for i in episodes_page) and any(i["UserData"]["IsFavorite"] for i in episodes_page)
    assert all(i["SeriesName"] and i["SeasonName"] == "第 1 季" for i in episodes_page)
    first_series = fast[1]["Items"][0]
    played = len(range(0, EPISODES, 3))  # 第一部劇的集照順序每三集看過一集
    assert first_series["ChildCount"] == EPISODES and first_series["UserData"]["UnplayedItemCount"] == EPISODES - played
    # 一頁 100 集：逐項查要 300 次以上（使用者資料、劇、季各一次），先查好只要個位數次
    assert slow_n[0] >= 300 and fast_n[0] <= 10, (slow_n, fast_n)
    # 一頁 100 部劇：每部原本要查使用者資料、子項數、沒看過幾集
    assert slow_n[1] >= 300 and fast_n[1] <= 10, (slow_n, fast_n)
    assert all(f <= s for f, s in zip(fast_n, slow_n)), (slow_n, fast_n)
