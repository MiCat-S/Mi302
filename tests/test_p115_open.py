"""115 開放平台通道測試，115 端以 httpx.MockTransport 模擬。"""

import json
import time

import httpx
import pytest

from embyserver.db import Database
from embyserver.p115 import P115Error, P115Service
from embyserver.p115_open import P115OpenClient, parse_download_url

from fakes import OPEN_CDN, OPEN_PC, FakeOpen115


def authorized_client():
    fake = FakeOpen115()
    client = P115OpenClient(Database(":memory:"), "app1", transport=httpx.MockTransport(fake))
    t = client.qrcode_start()
    assert t["uid"] == "u1" and "uid=u1" in t["qrcode_image"]
    assert client.qrcode_status("u1", "1", "s") == {"status": "success"}
    return client, fake


def test_pkce_authorization_and_download():
    client, fake = authorized_client()
    assert client.authorized
    assert client.download_url(OPEN_PC, "Infuse/8") == OPEN_CDN
    assert fake.calls[-1].headers["user-agent"] == "Infuse/8"


def test_expired_access_token_is_refreshed_and_retried():
    client, fake = authorized_client()
    fake.expire_next = True
    assert client.download_url(OPEN_PC) == OPEN_CDN
    assert any(c.url.path == "/open/refreshToken" for c in fake.calls)
    assert fake.calls[-1].headers["authorization"] == "Bearer at2"


def test_token_refreshed_before_expiry():
    client, fake = authorized_client()
    token = json.loads(client.db.get_meta("p115_open_token"))
    token["expires_at"] = int(time.time()) + 60  # 5 分鐘內到期
    client.db.set_meta("p115_open_token", json.dumps(token))
    client.download_url(OPEN_PC)
    assert json.loads(client.db.get_meta("p115_open_token"))["access_token"] == "at2"


def test_list_dir_and_dir_id():
    client, _ = authorized_client()
    assert client.dir_id("/影視") == 100
    items = client.list_dir(100)
    assert items[0] == {"name": "電影", "is_dir": True, "id": 101, "pickcode": "", "size": 0, "mtime": 0}
    assert items[1]["pickcode"] == OPEN_PC and items[1]["size"] == 1000 and not items[1]["is_dir"]


def test_parse_download_url_formats():
    assert parse_download_url(OPEN_CDN) == OPEN_CDN
    assert parse_download_url({"url": OPEN_CDN}) == OPEN_CDN
    assert parse_download_url({"url": {"url": OPEN_CDN}}) == OPEN_CDN
    assert parse_download_url({"9": {"url": {"url": OPEN_CDN}}}) == OPEN_CDN
    assert parse_download_url({"9": {"url": ""}}) is None


def test_service_prefers_open_and_falls_back_to_cookie(monkeypatch):
    svc = P115Service(Database(":memory:"), initial_cookies="UID=1", transport=httpx.MockTransport(FakeOpen115()))
    # 未授權開放平台：走 cookie
    monkeypatch.setattr(svc, "_cookie_download_url", lambda pc, ua: "https://cookie/" + pc)
    assert svc._fetch_download_url(OPEN_PC, "") == "https://cookie/" + OPEN_PC

    svc.open.default_app_id = "app1"
    svc.open.qrcode_start()
    svc.open.qrcode_status("u1", "1", "s")
    assert svc._fetch_download_url(OPEN_PC, "") == OPEN_CDN

    # 開放平台出錯時退回 cookie
    monkeypatch.setattr(svc.open, "download_url", lambda pc, ua: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert svc._fetch_download_url(OPEN_PC, "") == "https://cookie/" + OPEN_PC

    # 沒有 cookie 就回報錯誤
    svc.set_cookies("")
    with pytest.raises(P115Error):
        svc._fetch_download_url(OPEN_PC, "")


def test_expired_qrcode_is_reported():
    def fake(request):
        if request.url.path == "/get/status/":
            return httpx.Response(200, json={"state": 0, "message": "key invalid", "data": {}})
        return httpx.Response(404)

    client = P115OpenClient(Database(":memory:"), "app1", transport=httpx.MockTransport(fake))
    assert client.qrcode_status("u1", "1", "s") == {"status": "expired"}


def test_missing_dir_is_not_found_error():
    from embyserver.p115 import P115NotFound

    client, fake = authorized_client()
    with pytest.raises(P115NotFound):
        client.list_dir(555)  # 115 對不存在的 cid 靜默回根目錄
