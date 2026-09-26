"""以模擬 Emby 客戶端的請求順序測試：登入 → 媒體庫 → 項目 → PlaybackInfo → 302。"""

from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict

AUTH_HEADER = (
    'MediaBrowser Client="Infuse", Device="iPhone", DeviceId="dev-1", Version="8.0"'
)


@pytest.fixture()
def media(tmp_path: Path) -> Path:
    movies = tmp_path / "movies"
    (movies / "Inception (2010)").mkdir(parents=True)
    (movies / "Inception (2010)" / "Inception (2010).strm").write_text(
        "http://cdn.example.com/115/Inception.2010.mkv\n", encoding="utf-8"
    )
    (movies / "Inception (2010)" / "poster.jpg").write_bytes(b"\xff\xd8fakejpg")
    (movies / "Inception (2010)" / "movie.nfo").write_text(
        "<movie><title>全面啟動</title><year>2010</year><plot>夢境</plot>"
        "<uniqueid type='tmdb'>27205</uniqueid><genre>科幻</genre><runtime>148</runtime></movie>",
        encoding="utf-8",
    )
    (movies / "Local.Movie.2021.1080p.mp4").write_bytes(b"0123456789" * 100)
    (movies / "Rewrite (2022)").mkdir()
    (movies / "Rewrite (2022)" / "Rewrite (2022).strm").write_text(
        "/mnt/cloud/電影/Rewrite.mp4", encoding="utf-8"
    )

    tv = tmp_path / "tv"
    show = tv / "Dark (2017)"
    (show / "Season 1").mkdir(parents=True)
    (show / "Season 2").mkdir(parents=True)
    (show / "poster.jpg").write_bytes(b"\xff\xd8x")
    for s, e in ((1, 1), (1, 2), (2, 1)):
        (show / f"Season {s}" / f"Dark.S{s:02d}E{e:02d}.strm").write_text(
            f"http://cdn.example.com/dark/s{s}e{e}.mp4"
        )
    return tmp_path


@pytest.fixture()
def client(media: Path, tmp_path: Path):
    config = config_from_dict(
        {
            "server": {"name": "測試伺服器", "data_dir": str(tmp_path / "data")},
            "users": [{"name": "cat", "password": "secret", "admin": True}],
            "libraries": [
                {"name": "電影", "type": "movies", "paths": [str(media / "movies")]},
                {"name": "劇集", "type": "tvshows", "paths": [str(media / "tv")]},
            ],
            "redirect": {
                "path_rules": [{"from": "/mnt/cloud", "to": "http://alist.local:5244/d/cloud"}]
            },
        }
    )
    app = create_app(config, scan_on_start=False)
    app.state.scanner.scan_all()
    with TestClient(app) as c:
        yield c


def login(client) -> tuple[str, str]:
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": "cat", "Pw": "secret"},
        headers={"X-Emby-Authorization": AUTH_HEADER},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["AccessToken"], body["User"]["Id"]


def test_public_info_and_login(client):
    r = client.get("/emby/System/Info/Public")
    assert r.status_code == 200
    info = r.json()
    assert info["ServerName"] == "測試伺服器"
    assert info["ProductName"] == "Emby Server"

    assert client.get("/System/Info").status_code == 401
    bad = client.post("/Users/AuthenticateByName", json={"Username": "cat", "Pw": "no"})
    assert bad.status_code == 401

    token, user_id = login(client)
    r = client.get("/System/Info", headers={"X-Emby-Token": token})
    assert r.status_code == 200
    r = client.get(f"/Users/{user_id}", params={"api_key": token})
    assert r.json()["Name"] == "cat"
    assert r.json()["Policy"]["IsAdministrator"] is True


def test_views_and_movies(client):
    token, uid = login(client)
    h = {"X-Emby-Authorization": AUTH_HEADER + f', Token="{token}"'}
    views = client.get(f"/emby/Users/{uid}/Views", headers=h).json()
    names = {v["Name"]: v for v in views["Items"]}
    assert set(names) == {"電影", "劇集"}
    movies_lib = names["電影"]
    assert movies_lib["CollectionType"] == "movies"

    r = client.get(
        f"/emby/Users/{uid}/Items",
        params={
            "ParentId": movies_lib["Id"],
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
            "SortBy": "SortName",
            "Fields": "Overview,ProviderIds",
        },
        headers=h,
    )
    data = r.json()
    assert data["TotalRecordCount"] == 3
    by_name = {i["Name"]: i for i in data["Items"]}
    inception = by_name["全面啟動"]
    assert inception["ProductionYear"] == 2010
    assert inception["ProviderIds"] == {"Tmdb": "27205"}
    assert "Primary" in inception["ImageTags"]
    assert "Local Movie" in by_name

    img = client.get(f"/Items/{inception['Id']}/Images/Primary")
    assert img.status_code == 200 and img.content.startswith(b"\xff\xd8")


def test_strm_302(client):
    token, uid = login(client)
    h = {"X-Emby-Token": token}
    items = client.get(
        f"/Users/{uid}/Items", params={"Recursive": "true", "IncludeItemTypes": "Movie", "SearchTerm": "全面"}, headers=h
    ).json()["Items"]
    item_id = items[0]["Id"]

    pb = client.post(f"/Items/{item_id}/PlaybackInfo", params={"UserId": uid}, json={}, headers=h).json()
    ms = pb["MediaSources"][0]
    assert ms["Protocol"] == "Http" and ms["IsRemote"] is True
    assert ms["Path"] == "http://cdn.example.com/115/Inception.2010.mkv"
    assert ms["SupportsDirectPlay"] and not ms["SupportsTranscoding"]
    assert ms["Container"] == "mkv"

    # 播放器照 DirectStreamUrl 請求，不帶 token 也要 302
    r = client.get(ms["DirectStreamUrl"].split("&api_key")[0], follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "http://cdn.example.com/115/Inception.2010.mkv"

    for path in (
        f"/emby/videos/{item_id}/original.mkv",
        f"/Videos/{item_id}/stream",
    ):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302, path

    # 下載不是播放：要登入
    for path in (f"/Items/{item_id}/Download", f"/Items/{item_id}/File"):
        assert client.get(path, follow_redirects=False).status_code == 401, path
        r = client.get(path, params={"api_key": token}, follow_redirects=False)
        assert r.status_code == 302, path


def test_strm_path_rule(client):
    token, uid = login(client)
    h = {"X-Emby-Token": token}
    item = client.get(
        "/Items", params={"Recursive": "true", "SearchTerm": "Rewrite"}, headers=h
    ).json()["Items"][0]
    r = client.get(f"/Videos/{item['Id']}/stream.mp4", follow_redirects=False)
    assert r.status_code == 302
    # Location 標頭中的非 ASCII 字元會被百分比編碼
    assert unquote(r.headers["location"]) == "http://alist.local:5244/d/cloud/電影/Rewrite.mp4"


def test_local_file_stream_with_range(client):
    token, uid = login(client)
    h = {"X-Emby-Token": token}
    item = client.get(
        "/Items", params={"Recursive": "true", "SearchTerm": "Local"}, headers=h
    ).json()["Items"][0]
    r = client.get(f"/Videos/{item['Id']}/stream", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206
    assert r.content == b"0123456789"


def test_tv_shows(client):
    token, uid = login(client)
    h = {"X-Emby-Token": token}
    series = client.get(
        f"/Users/{uid}/Items", params={"Recursive": "true", "IncludeItemTypes": "Series"}, headers=h
    ).json()["Items"]
    assert [s["Name"] for s in series] == ["Dark"]
    sid = series[0]["Id"]
    seasons = client.get(f"/Shows/{sid}/Seasons", params={"UserId": uid}, headers=h).json()["Items"]
    assert [s["IndexNumber"] for s in seasons] == [1, 2]
    eps = client.get(
        f"/Shows/{sid}/Episodes", params={"SeasonId": seasons[0]["Id"], "UserId": uid}, headers=h
    ).json()["Items"]
    assert [(e["ParentIndexNumber"], e["IndexNumber"]) for e in eps] == [(1, 1), (1, 2)]
    assert eps[0]["SeriesName"] == "Dark"
    assert eps[0]["SeriesPrimaryImageTag"]

    r = client.get(f"/Videos/{eps[1]['Id']}/stream", follow_redirects=False)
    assert r.headers["location"] == "http://cdn.example.com/dark/s1e2.mp4"


def test_progress_and_played(client):
    token, uid = login(client)
    h = {"X-Emby-Token": token}
    item = client.get(
        "/Items", params={"Recursive": "true", "SearchTerm": "全面"}, headers=h
    ).json()["Items"][0]
    iid = item["Id"]
    r = client.post(
        "/Sessions/Playing/Progress", json={"ItemId": iid, "PositionTicks": 600_000_000}, headers=h
    )
    assert r.status_code == 204
    resume = client.get(f"/Users/{uid}/Items/Resume", headers=h).json()["Items"]
    assert resume[0]["UserData"]["PlaybackPositionTicks"] == 600_000_000

    # 148 分鐘，停在結尾 → 自動標記已看
    end = 148 * 60 * 10_000_000
    client.post("/Sessions/Playing/Stopped", json={"ItemId": iid, "PositionTicks": end}, headers=h)
    got = client.get(f"/Users/{uid}/Items/{iid}", headers=h).json()
    assert got["UserData"]["Played"] is True

    r = client.post(f"/Users/{uid}/FavoriteItems/{iid}", headers=h)
    assert r.json()["IsFavorite"] is True


def test_form_login(client):
    r = client.post("/Users/AuthenticateByName", data={"Username": "cat", "Pw": "secret"})
    assert r.status_code == 200 and r.json()["AccessToken"]
    r = client.post("/Users/AuthenticateByName", data={"Username": "cat", "Pw": "wrong"})
    assert r.status_code == 401


def test_start_without_position_keeps_resume_point(client):
    token, uid = login(client)
    h = {"X-Emby-Token": token}
    iid = client.get("/Items", params={"Recursive": "true", "SearchTerm": "全面"}, headers=h).json()["Items"][0]["Id"]
    client.post("/Sessions/Playing/Progress", json={"ItemId": iid, "PositionTicks": 600_000_000}, headers=h)
    before = client.get(f"/Users/{uid}/Items/{iid}", headers=h).json()["UserData"]["LastPlayedDate"]
    assert client.post(f"/Users/{uid}/PlayingItems/{iid}", headers=h).status_code == 204  # 舊版 API 不帶位置
    assert client.post("/Sessions/Playing", json={"ItemId": iid}, headers=h).status_code == 204
    data = client.get(f"/Users/{uid}/Items/{iid}", headers=h).json()["UserData"]
    assert data["PlaybackPositionTicks"] == 600_000_000
    assert data["LastPlayedDate"] >= before
    # 帶了位置照常更新
    client.post(f"/Users/{uid}/PlayingItems/{iid}/Progress", params={"PositionTicks": 700_000_000}, headers=h)
    assert client.get(f"/Users/{uid}/Items/{iid}", headers=h).json()["UserData"]["PlaybackPositionTicks"] == 700_000_000


def test_other_users_data(client):
    admin_token, admin_id = login(client)
    kid_id = client.app.state.auth.create_user("kid", "pw", False)["id"]
    kid_token = client.post("/Users/AuthenticateByName", json={"Username": "kid", "Pw": "pw"}).json()["AccessToken"]
    admin, kid = {"X-Emby-Token": admin_token}, {"X-Emby-Token": kid_token}
    params = {"Recursive": "true", "SearchTerm": "全面"}
    iid = client.get("/Items", params=params, headers=kid).json()["Items"][0]["Id"]
    client.post(f"/Users/{kid_id}/PlayedItems/{iid}", headers=kid)

    # 非管理員：只能查自己
    assert client.get(f"/Users/{admin_id}", headers=kid).status_code == 403
    assert client.get(f"/Users/{kid_id}", headers=kid).json()["Name"] == "kid"
    assert client.get("/Items", params={**params, "UserId": admin_id}, headers=kid).status_code == 403
    assert client.get(f"/Users/{admin_id}/Items", params=params, headers=kid).status_code == 403
    assert client.get("/Items", params={**params, "UserId": kid_id}, headers=kid).status_code == 200

    # 管理員代查：用那個人的播放紀錄
    assert client.get(f"/Users/{kid_id}", headers=admin).json()["Name"] == "kid"
    played = lambda r: r.json()["Items"][0]["UserData"]["Played"]  # noqa: E731
    assert played(client.get("/Items", params=params, headers=admin)) is False
    assert played(client.get("/Items", params={**params, "UserId": kid_id}, headers=admin)) is True
    assert played(client.get(f"/Users/{kid_id}/Items", params=params, headers=admin)) is True
    assert client.get("/Items", params={**params, "IsPlayed": "true", "UserId": kid_id}, headers=admin).json()["TotalRecordCount"] == 1
    assert client.get("/Items", params={**params, "UserId": "nobody"}, headers=admin).status_code == 404


def test_resolve_redirect_chain(media: Path, tmp_path: Path):
    """resolve_redirects 開啟時，先跟隨上游重導向，把最終網址交給播放器。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Upstream(BaseHTTPRequestHandler):
        def do_HEAD(self):
            if self.path.startswith("/d/"):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port}/final/signed?sig=abc")
            else:
                self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Upstream)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    strm = media / "movies" / "Inception (2010)" / "Inception (2010).strm"
    strm.write_text(f"http://127.0.0.1:{port}/d/Inception.mkv")
    config = config_from_dict(
        {
            "server": {"data_dir": str(tmp_path / "data2")},
            "users": [{"name": "cat", "password": "secret"}],
            "libraries": [{"name": "電影", "type": "movies", "paths": [str(media / "movies")]}],
            "redirect": {"resolve_redirects": True},
        }
    )
    app = create_app(config, scan_on_start=False)
    app.state.scanner.scan_all()
    try:
        with TestClient(app) as c:
            token, _ = login(c)
            item = c.get(
                "/Items", params={"Recursive": "true", "SearchTerm": "全面"}, headers={"X-Emby-Token": token}
            ).json()["Items"][0]
            r = c.get(f"/Videos/{item['Id']}/stream", follow_redirects=False)
            assert r.headers["location"] == f"http://127.0.0.1:{port}/final/signed?sig=abc"
    finally:
        server.shutdown()
