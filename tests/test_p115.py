"""115 掃碼登入與 pickcode 302 的測試，115 端以 httpx.MockTransport 模擬。"""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.db import Database
from embyserver.p115 import P115Service, extract_pickcode

PICKCODE = "abcdefghijklmnopq"
PC_UP = PICKCODE.upper()
CDN = "https://cdnfhnfile.115cdn.net/abc/Inception.mkv?t=4102444800&u=1"


def test_extract_pickcode():
    assert extract_pickcode(f"http://nas:8096/d/{PC_UP}.mkv") == PICKCODE
    assert extract_pickcode(f"http://nas:8096/d/{PICKCODE}.mkv?/Inception.mkv") == PICKCODE
    assert extract_pickcode(f"http://nas:8096/d/{PICKCODE}") == PICKCODE
    assert extract_pickcode(f"http://nas:8096/d/{PICKCODE}.mkv/Inception.mkv") == PICKCODE
    assert extract_pickcode("http://alist:5244/d/電影/Inception.mkv") is None
    assert extract_pickcode(
        f"http://mp:3000/api/v1/plugin/P115StrmHelper/redirect_url?pickcode={PICKCODE}&file_name=a.mkv"
    ) == PICKCODE
    assert extract_pickcode(f"http://host:8096/p115/redirect?pickcode={PICKCODE.upper()}") == PICKCODE
    assert extract_pickcode(f"115://{PICKCODE}") == PICKCODE
    assert extract_pickcode("http://cdn.example.com/a.mkv") is None
    assert extract_pickcode("http://x/redirect_url?pickcode=short") is None


def test_qrcode_login_flow():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/1.0/web/1.0/token/":
            return httpx.Response(200, json={"state": 1, "data": {"uid": "u1", "time": 1, "sign": "s"}})
        if request.url.path == "/get/status/":
            return httpx.Response(200, json={"state": 1, "data": {"status": 2}})
        if request.url.path == "/app/1.0/alipaymini/1.0/login/qrcode/":
            assert request.content == b"account=u1"
            return httpx.Response(
                200, json={"state": 1, "data": {"cookie": {"UID": "1_A1", "CID": "c", "SEID": "s"}}}
            )
        if request.url.host == "my.115.com":
            assert "UID=1_A1" in request.headers["cookie"]
            return httpx.Response(200, json={"state": True, "data": {"user_id": 1, "user_name": "cat"}})
        return httpx.Response(404)

    svc = P115Service(Database(":memory:"), transport=httpx.MockTransport(handler))
    token = svc.qrcode_token()
    assert token["uid"] == "u1" and "qrcode?uid=u1" in token["qrcode_image"]
    result = svc.qrcode_status("u1", "1", "s")
    assert result["status"] == "success"
    assert result["user"]["user_name"] == "cat"
    assert svc.cookies == "UID=1_A1; CID=c; SEID=s"


def test_download_request_is_encrypted_and_cached(monkeypatch):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"state": True, "data": "ENC"})

    svc = P115Service(Database(":memory:"), initial_cookies="UID=1", transport=httpx.MockTransport(handler))
    import p115cipher

    monkeypatch.setattr(p115cipher, "rsa_decrypt", lambda data: b'{"url": {"url": "%s"}}' % CDN.encode())
    assert svc.download_url(PICKCODE, "Infuse/8") == CDN
    assert svc.download_url(PICKCODE, "Infuse/8") == CDN  # 第二次走快取
    assert len(sent) == 1
    req = sent[0]
    assert req.url.host == "proapi.115.com"
    assert req.headers["user-agent"] == "Infuse/8"
    assert req.headers["cookie"] == "UID=1"
    assert req.content.startswith(b"data=")
    # 不同 UA 的直鏈不能共用
    svc.download_url(PICKCODE, "VidHub/2")
    assert len(sent) == 2


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    movie = tmp_path / "movies" / "Inception (2010)"
    movie.mkdir(parents=True)
    (movie / "Inception (2010).strm").write_text(f"http://nas:8096/d/{PICKCODE}.mkv")
    config = config_from_dict(
        {
            "server": {"data_dir": str(tmp_path / "data")},
            "users": [{"name": "cat", "password": "pw", "admin": True}],
            "libraries": [{"name": "電影", "type": "movies", "paths": [str(tmp_path / "movies")]}],
            "p115": {"cookies": "UID=1; CID=2; SEID=3"},
        }
    )
    app = create_app(config, scan_on_start=False)
    app.state.scanner.scan_all()
    seen = []

    def fake_fetch(pickcode, ua):
        seen.append((pickcode, ua))
        return CDN

    monkeypatch.setattr(app.state.p115, "_fetch_download_url", fake_fetch)
    with TestClient(app) as c:
        c.seen = seen
        yield c


def _login(c):
    r = c.post("/Users/AuthenticateByName", json={"Username": "cat", "Pw": "pw"})
    return r.json()["AccessToken"]


def test_strm_pickcode_redirects_via_115(client):
    token = _login(client)
    h = {"X-Emby-Token": token}
    item = client.get("/Items", params={"Recursive": "true", "IncludeItemTypes": "Movie"}, headers=h).json()["Items"][0]
    pb = client.post(f"/Items/{item['Id']}/PlaybackInfo", json={}, headers=h).json()
    ms = pb["MediaSources"][0]
    # Path 指回本伺服器，確保播放器不會繞過伺服器直接打 MoviePilot
    assert ms["Path"].startswith("http://testserver/videos/")
    assert ms["IsRemote"] is True

    r = client.get(f"/videos/{item['Id']}/stream.mkv", headers={"User-Agent": "Infuse/8"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == CDN
    assert client.seen == [(PICKCODE, "Infuse/8")]


def test_short_link_endpoint(client):
    for path in (f"/d/{PICKCODE}.mkv", f"/d/{PICKCODE}.mkv?/Inception.mkv", f"/d/{PICKCODE}/Inception.mkv"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == CDN, path
    assert client.get("/d/bad.mkv", follow_redirects=False).status_code == 400


def test_other_tools_strm_endpoint(client):
    r = client.get(
        f"/api/v1/plugin/P115StrmHelper/redirect_url?pickcode={PICKCODE}", follow_redirects=False
    )
    assert r.status_code == 302 and r.headers["location"] == CDN
    assert client.get("/p115/redirect?pickcode=bad", follow_redirects=False).status_code == 400


def test_admin_endpoints_require_admin(client):
    assert client.get("/p115/status").status_code == 401
    token = _login(client)
    assert client.get("/p115/status", headers={"X-Emby-Token": token}).json()["logged_in"] is True
    assert client.get("/web/115").status_code == 200
