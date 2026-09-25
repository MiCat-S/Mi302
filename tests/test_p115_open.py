"""115 開放平台通道測試，115 端以 httpx.MockTransport 模擬。"""

import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from embyserver.db import Database
from embyserver.p115 import P115Error, P115Service
from embyserver.p115_open import P115OpenClient, parse_download_url

PC = "abcdefghijklmnopq"
CDN = "https://cdnfhnfile.115cdn.net/x/a.mkv?t=4102444800"


class Fake115:
    def __init__(self):
        self.calls = []
        self.token_version = 1
        self.expire_next = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        form = parse_qs(request.content.decode()) if request.content else {}
        if path == "/open/authDeviceCode":
            assert form["client_id"] == ["app1"] and form["code_challenge_method"] == ["sha256"]
            self.challenge = form["code_challenge"][0]
            return httpx.Response(200, json={"state": 1, "data": {"uid": "u1", "time": 1, "sign": "s"}})
        if path == "/get/status/":
            return httpx.Response(200, json={"state": 1, "data": {"status": 2}})
        if path == "/open/deviceCodeToToken":
            import base64, hashlib
            verifier = form["code_verifier"][0]
            expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            assert expect == self.challenge  # PKCE 驗證
            return httpx.Response(200, json={"state": 1, "data": {"access_token": "at1", "refresh_token": "rt1", "expires_in": 7200}})
        if path == "/open/refreshToken":
            self.token_version += 1
            return httpx.Response(200, json={"state": 1, "data": {
                "access_token": f"at{self.token_version}", "refresh_token": f"rt{self.token_version}", "expires_in": 7200}})
        auth = request.headers.get("authorization", "")
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(200, json={"state": 0, "code": 40140125, "message": "token expired"})
        if path == "/open/ufile/downurl":
            assert form["pick_code"] == [PC] and auth.startswith("Bearer at")
            return httpx.Response(200, json={"state": True, "data": {"123": {"url": {"url": CDN}}}})
        if path == "/open/folder/get_info":
            return httpx.Response(200, json={"state": True, "data": {"file_id": "100"}})
        if path == "/open/ufile/files":
            return httpx.Response(200, json={"state": True, "count": 2, "path": [{"cid": 0}, {"cid": 100}], "data": [
                {"fid": "101", "fn": "電影", "fc": "0"},
                {"fid": "5", "fn": "a.mkv", "fc": "1", "pc": PC, "fs": 1000},
            ]})
        return httpx.Response(404)


def authorized_client():
    fake = Fake115()
    client = P115OpenClient(Database(":memory:"), "app1", transport=httpx.MockTransport(fake))
    t = client.qrcode_start()
    assert t["uid"] == "u1" and "uid=u1" in t["qrcode_image"]
    assert client.qrcode_status("u1", "1", "s") == {"status": "success"}
    return client, fake


def test_pkce_authorization_and_download():
    client, fake = authorized_client()
    assert client.authorized
    assert client.download_url(PC, "Infuse/8") == CDN
    assert fake.calls[-1].headers["user-agent"] == "Infuse/8"


def test_expired_access_token_is_refreshed_and_retried():
    client, fake = authorized_client()
    fake.expire_next = True
    assert client.download_url(PC) == CDN
    assert any(c.url.path == "/open/refreshToken" for c in fake.calls)
    assert fake.calls[-1].headers["authorization"] == "Bearer at2"


def test_token_refreshed_before_expiry():
    client, fake = authorized_client()
    token = json.loads(client.db.get_meta("p115_open_token"))
    token["expires_at"] = int(time.time()) + 60  # 5 分鐘內到期
    client.db.set_meta("p115_open_token", json.dumps(token))
    client.download_url(PC)
    assert json.loads(client.db.get_meta("p115_open_token"))["access_token"] == "at2"


def test_list_dir_and_dir_id():
    client, _ = authorized_client()
    assert client.dir_id("/影視") == 100
    items = client.list_dir(100)
    assert items[0] == {"name": "電影", "is_dir": True, "id": 101, "pickcode": "", "size": 0, "mtime": 0}
    assert items[1]["pickcode"] == PC and items[1]["size"] == 1000 and not items[1]["is_dir"]


def test_parse_download_url_formats():
    assert parse_download_url(CDN) == CDN
    assert parse_download_url({"url": CDN}) == CDN
    assert parse_download_url({"url": {"url": CDN}}) == CDN
    assert parse_download_url({"9": {"url": {"url": CDN}}}) == CDN
    assert parse_download_url({"9": {"url": ""}}) is None


def test_service_prefers_open_and_falls_back_to_cookie(monkeypatch):
    svc = P115Service(Database(":memory:"), initial_cookies="UID=1", transport=httpx.MockTransport(Fake115()))
    # 未授權開放平台：走 cookie
    monkeypatch.setattr(svc, "_cookie_download_url", lambda pc, ua: "https://cookie/" + pc)
    assert svc._fetch_download_url(PC, "") == "https://cookie/" + PC

    svc.open.default_app_id = "app1"
    svc.open.qrcode_start()
    svc.open.qrcode_status("u1", "1", "s")
    assert svc._fetch_download_url(PC, "") == CDN

    # 開放平台出錯時退回 cookie
    monkeypatch.setattr(svc.open, "download_url", lambda pc, ua: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert svc._fetch_download_url(PC, "") == "https://cookie/" + PC

    # 沒有 cookie 就回報錯誤
    svc.set_cookies("")
    with pytest.raises(P115Error):
        svc._fetch_download_url(PC, "")
