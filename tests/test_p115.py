"""115 掃碼登入與 pickcode 302 的測試，115 端以 httpx.MockTransport 模擬。"""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.db import Database
from embyserver.p115 import P115Error, P115Service, extract_pickcode

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
    import embyserver.p115 as mod

    monkeypatch.setattr(mod, "rsa_decrypt", lambda data: b'{"url": {"url": "%s"}}' % CDN.encode())
    assert svc.download_url(PICKCODE, "Infuse/8") == CDN
    assert svc.download_url(PICKCODE, "Infuse/8") == CDN  # 第二次走快取
    assert len(sent) == 1
    req = sent[0]
    assert (req.url.scheme, req.url.host) == ("https", "proapi.115.com")  # 帶 cookie 的請求不能走明文 http
    assert req.headers["user-agent"] == "Infuse/8"
    assert req.headers["cookie"] == "UID=1"
    assert req.content.startswith(b"data=")
    # 不同 UA 的直鏈不能共用
    svc.download_url(PICKCODE, "VidHub/2")
    assert len(sent) == 2
    # UA 裡有中文（Starlette 用 latin-1 解成 str）：照播放器送來的位元組原樣送給 115，以前在送出前就丟 UnicodeEncodeError
    svc.download_url(PICKCODE, "播放器/1.0".encode().decode("latin-1"))
    assert dict(sent[2].headers.raw)[b"User-Agent"] == "播放器/1.0".encode()
    # 自己下載 115 檔案時，cookie 只給 https 的 115 網域
    assert svc.file_headers(CDN)["Cookie"] == "UID=1"
    for url in ("http://cdnfhnfile.115cdn.net/abc/a.mkv", "https://evil115.com/a", "https://115.com.evil.net/a"):
        assert "Cookie" not in svc.file_headers(url), url



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

    r = client.get(f"/videos/{item['Id']}/stream.mkv", headers={**h, "User-Agent": "Infuse/8"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == CDN
    assert client.seen == [(PICKCODE, "Infuse/8")]


def test_short_link_endpoint(client):
    for path in (f"/d/{PICKCODE}.mkv", f"/d/{PICKCODE}.mkv?/Inception.mkv", f"/d/{PICKCODE}/Inception.mkv"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == CDN, path
    assert client.get("/d/bad.mkv", follow_redirects=False).status_code == 400
    # 不用登入就能打：亂打 pickcode 的來源，取不到 10 次之後先回 429，不再替它問 115；剛取過的照給
    asked = []

    def missing(pickcode, ua):
        asked.append(pickcode)
        raise P115Error("文件不存在")

    client.app.state.p115._fetch_download_url = missing
    codes = [client.get(f"/d/{i:017d}.mkv", follow_redirects=False).status_code for i in range(12)]
    assert codes == [502] * 10 + [429] * 2 and len(asked) == 10
    assert client.get(f"/p115/redirect?pickcode={99:017d}", follow_redirects=False).headers["retry-after"]
    assert client.get(f"/d/{PICKCODE}.mkv", follow_redirects=False).status_code == 302 and len(asked) == 10


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


def test_strm_tasks_from_web_and_auto_base_url(client, tmp_path: Path):
    h = {"X-Emby-Token": _login(client)}
    s = client.get("/p115/strm/status", headers=h).json()
    assert s["tasks"] == [] and s["libraries"][0]["name"] == "電影"
    # 沒填 base_url 時，用管理員開網頁的網址
    client.get("/p115/status", headers=h)
    assert client.get("/p115/strm/status", headers=h).json()["base_url"] == "http://testserver"

    body = [
        {"remote": "影視/電影", "local": str(tmp_path / "movies" / "115")},
        {"remote": "/影視/劇集", "local": str(tmp_path / "elsewhere")},
    ]
    tasks = client.put("/p115/strm/tasks", json=body, headers=h).json()["tasks"]
    assert [t["remote"] for t in tasks] == ["/影視/電影", "/影視/劇集"]
    assert [t["in_library"] for t in tasks] == [True, False]
    assert client.get("/p115/strm/status", headers=h).json()["tasks"] == tasks
    assert client.put("/p115/strm/tasks", json=[{"remote": "/a"}], headers=h).status_code == 400


def test_network_error_is_reported_not_500():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known", request=request)

    svc = P115Service(Database(":memory:"), transport=httpx.MockTransport(handler))
    with pytest.raises(P115Error, match="連不到 qrcodeapi.115.com：無法連線"):
        svc.qrcode_token()


def test_rsa_decrypt_inverts_115_scheme(monkeypatch):
    """115 用私鑰加密回應，測試時略過 RSA 那層，只驗證外層的 xor／反轉是否還原正確。

    p115cipher 內建的 rsa_decrypt 在這一步會丟 memoryview cast 的 TypeError。
    """
    import os

    import p115cipher.util as util
    from p115cipher import RSA_KEY
    import embyserver.p115 as mod

    monkeypatch.setattr(util, "rsa_decrypt_with_pubkey", lambda b: bytearray(b))
    msg = b'{"url": {"url": "https://cdn.115.com/a.mkv?t=1"}}'
    rand_key = os.urandom(16)
    inner = bytes(util.xor(msg, RSA_KEY))[::-1]
    payload = rand_key + bytes(util.xor(inner, util.rsa_gen_key(rand_key, 12)))
    import base64

    assert mod.rsa_decrypt(base64.b64encode(payload)) == msg
