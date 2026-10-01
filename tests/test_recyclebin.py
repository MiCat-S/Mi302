"""115 回收站：列出、清空（開放平台優先，cookie 要安全密鑰），網頁 API 沒帶「清空」不做。"""

from urllib.parse import parse_qs

import httpx
import pytest

from embyserver.db import Database
from embyserver.p115 import P115Error, P115Service, parse_recycle_bin

from fakes import FakeOpen115, admin_headers, make_client

COOKIE_ITEMS = [
    {"id": "11", "file_name": "Dark.S01E01.mkv", "file_size": "900000000", "dtime": "1700000000", "parent_name": "Dark"},
    {"id": "12", "file_name": "舊資料夾", "type": "2", "dtime": "1700000100", "parent_name": "影視"},
]


class Fake115(FakeOpen115):
    """開放平台（授權、回收站）加上 cookie 的回收站。"""

    def __init__(self, key: str = "123456"):
        super().__init__()
        self.key = key
        self.cleaned = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        form = parse_qs(request.content.decode(), keep_blank_values=True) if request.content else {}
        if request.url.host == "webapi.115.com" and path == "/rb":
            assert request.url.params["aid"] == "7"
            return httpx.Response(200, json={"state": True, "count": "2", "data": COOKIE_ITEMS})
        if request.url.host == "webapi.115.com" and path == "/rb/secret_del":
            assert form["tid"] == [""]  # 不帶檔案 = 全部清空
            if form["password"] != [self.key]:
                return httpx.Response(200, json={"state": False, "error": "安全密钥错误"})
            self.cleaned.append("cookie")
            return httpx.Response(200, json={"state": True})
        if path == "/open/rb/list":
            self.calls.append(request)
            return httpx.Response(200, json={"state": True, "code": 0, "data": {
                "offset": 0, "limit": 50, "count": "1", "rb_pass": 1,
                "0": {"id": "21", "file_name": "Up (2009).mkv", "file_size": 1000, "dtime": 1700000200, "parent_name": "電影"}}})
        if path == "/open/rb/del":
            self.calls.append(request)
            assert "password" not in form and "tid" not in form
            self.cleaned.append("open")
            return httpx.Response(200, json={"state": True, "code": 0, "data": []})
        return super().__call__(request)


def test_parse_recycle_bin_both_formats():
    cookie = parse_recycle_bin({"count": "2", "data": COOKIE_ITEMS})
    assert cookie["count"] == 2 and cookie["items"][0] == {
        "id": "11", "name": "Dark.S01E01.mkv", "size": 900000000, "dtime": 1700000000, "parent": "Dark"}
    assert cookie["items"][1]["size"] == 0
    opened = parse_recycle_bin({"data": {"count": "1", "rb_pass": 1, "0": {"id": "21", "file_name": "a.mkv"}}})
    assert opened == {"count": 1, "items": [{"id": "21", "name": "a.mkv", "size": 0, "dtime": 0, "parent": ""}]}
    assert parse_recycle_bin({"data": []}) == {"count": 0, "items": []}


def test_cookie_clean_needs_the_security_key():
    fake = Fake115()
    svc = P115Service(Database(":memory:"), initial_cookies="UID=1", transport=httpx.MockTransport(fake))
    assert [i["name"] for i in svc.recycle_bin()["items"]] == ["Dark.S01E01.mkv", "舊資料夾"]
    with pytest.raises(P115Error, match="安全密钥错误"):
        svc.recycle_bin_clean("654321")
    with pytest.raises(P115Error, match="6 位數字"):
        svc.recycle_bin_clean("12a")
    assert not fake.cleaned
    assert svc.recycle_bin_clean("123456") == "cookie" and fake.cleaned == ["cookie"]
    # 在 115 關掉「清空要密鑰」時不填：照 115 網頁的做法送 000000
    fake.key = "000000"
    assert svc.recycle_bin_clean("") == "cookie"


def test_open_platform_clean_needs_no_key():
    fake = Fake115()
    svc = P115Service(Database(":memory:"), initial_cookies="UID=1", transport=httpx.MockTransport(fake))
    svc.open.default_app_id = "app1"
    svc.open.qrcode_start()
    svc.open.qrcode_status("u1", "1", "s")
    page = svc.recycle_bin()
    assert page["count"] == 1 and page["items"][0]["name"] == "Up (2009).mkv"
    assert svc.recycle_bin_clean() == "open" and fake.cleaned == ["open"]


def test_web_api_requires_explicit_confirmation(tmp_path):
    c = make_client(tmp_path, {"users": [{"name": "admin", "password": "pw", "admin": True}]})
    h = admin_headers(c)
    fake = Fake115()
    c.app.state.p115 = P115Service(c.app.state.db, initial_cookies="UID=1", transport=httpx.MockTransport(fake))
    r = c.get("/web/api/115/recyclebin", headers=h).json()
    assert (r["count"], r["via"], len(r["items"])) == (2, "cookie", 2)
    assert c.post("/web/api/115/recyclebin/clean", json={"password": "123456"}, headers=h).status_code == 400
    assert c.post("/web/api/115/recyclebin/clean", json={"confirm": "好", "password": "123456"}, headers=h).status_code == 400
    bad = c.post("/web/api/115/recyclebin/clean", json={"confirm": "清空", "password": "000001"}, headers=h)
    assert bad.status_code == 400 and "安全密钥错误" in bad.text
    assert not fake.cleaned
    ok = c.post("/web/api/115/recyclebin/clean", json={"confirm": "清空", "password": "123456"}, headers=h)
    assert ok.json() == {"ok": True, "via": "cookie"} and fake.cleaned == ["cookie"]
    assert c.post("/web/api/115/recyclebin/clean", json={"confirm": "清空"}).status_code == 401
