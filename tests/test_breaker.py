"""115 熔斷：被限流或登入失效時停下背景工作，播放照常。"""

import time
from pathlib import Path

import httpx
import pytest

from embyserver.db import Database
from embyserver.p115 import P115Error, P115Service, P115Throttled
from embyserver.strm_sync import FULL

from test_incremental import Fake115, make


def service(handler) -> P115Service:
    return P115Service(Database(":memory:"), initial_cookies="UID=1", transport=httpx.MockTransport(handler))


def test_http_405_trips_and_cools_down():
    svc = service(lambda r: httpx.Response(405, text="<html>405 Not Allowed</html>"))
    with pytest.raises(P115Throttled):
        svc._webapi_get("/files", {"cid": 0})
    assert svc.breaker.tripped and svc.breaker.status()["until"]
    with pytest.raises(P115Throttled, match="約 \\d+ 分鐘後再試"):
        svc.breaker.check()
    svc.breaker.tripped_at = time.time() - svc.breaker.cooldown - 1  # 冷卻期滿自動恢復
    assert not svc.breaker.tripped
    svc.breaker.check()


def test_errno_and_messages_trip():
    svc = service(lambda r: httpx.Response(200, json={"state": False, "errno": 770004, "error": "访问上限"}))
    with pytest.raises(P115Error):
        svc._webapi_get("/files", {"cid": 0})
    assert svc.breaker.tripped and not svc.breaker.login_bad

    # 一般錯誤（例如目錄不存在）不算限流
    svc2 = service(lambda r: httpx.Response(200, json={"state": False, "errno": 20004, "error": "参数错误"}))
    with pytest.raises(P115Error):
        svc2._webapi_get("/files", {"cid": 0})
    assert not svc2.breaker.tripped


def test_login_invalid_stays_until_new_cookie():
    svc = service(lambda r: httpx.Response(200, json={"state": False, "errno": 990001, "error": "登录超时，请重新登录"}))
    with pytest.raises(P115Error):
        svc._webapi_post("/files/export_dir", {"file_ids": 1})
    assert svc.breaker.login_bad
    svc.breaker.tripped_at = time.time() - svc.breaker.cooldown - 1
    assert svc.breaker.tripped  # 登入失效不會自己好
    assert "重新掃碼" in svc.breaker.status()["message"]
    svc.set_cookies("UID=2")
    assert not svc.breaker.tripped


def test_playback_link_is_not_blocked():
    svc = service(lambda r: httpx.Response(405))
    svc.breaker.trip("HTTP 405")
    svc._fetch_download_url = lambda pc, ua: "https://cdn.115.test/x"
    assert svc.download_url("a" * 17, "Infuse") == "https://cdn.115.test/x"


def test_sync_waits_while_tripped(tmp_path: Path):
    fake = Fake115()
    sync = make(tmp_path, fake)
    sync.p115.breaker.trip("HTTP 405")
    r = sync.run(FULL)
    assert r.strm_created == 0 and not r.errors and "不同步" in r.notes[0]
    assert fake.calls == []  # 一個請求都沒打


def test_throttled_during_sync_does_not_fall_back_to_walk(tmp_path: Path):
    fake = Fake115()
    sync = make(tmp_path, fake)
    real = fake.handler

    def throttled(request):
        if request.url.path == "/files" and request.url.params.get("cur") == "0":
            return httpx.Response(405, text="<html>405</html>")  # 列檔案時被限流
        return real(request)

    sync.p115._client._transport = httpx.MockTransport(throttled)
    r = sync.run(FULL)
    assert sync.p115.breaker.tripped
    assert r.errors and "限流" in r.errors[0]
    # 沒有改成逐層列目錄，免得打得更多
    assert not [c for c in fake.calls if c[0] == "/files" and c[1].get("cur") == "1" and c[1].get("limit") != "1"]


def test_status_api_reports_breaker(tmp_path: Path):
    from fastapi.testclient import TestClient

    from embyserver.app import create_app
    from embyserver.config import config_from_dict

    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")},
        "users": [{"name": "admin", "password": "pw", "admin": True}],
    }), scan_on_start=False)
    c = TestClient(app)
    token = c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]
    app.state.p115.breaker.trip("errno 770004")
    s = c.get("/p115/status", headers={"X-Emby-Token": token}).json()
    assert s["breaker"]["tripped"] and "限流" in s["breaker"]["message"]


def test_error_reply_with_empty_data_is_not_an_empty_listing(tmp_path: Path):
    """115 回錯誤時常常帶著 data: []，不能當成「資料夾是空的」，否則 delete_stale 會把本機 strm 全刪掉。"""
    fake = Fake115()
    sync = make(tmp_path, fake, delete_stale=True)
    assert sync.run(FULL).strm_created == 2
    strm = tmp_path / "media" / "電影" / "Old Movie (2001).strm"
    fake.export_ok = False  # 導出目錄樹不行，改成逐層列目錄
    real = fake.handler

    def expired(request):
        if request.url.path == "/files" and request.url.params.get("cur") == "1":
            return httpx.Response(200, json={"state": False, "errNo": 990001, "error": "登录超时，请重新登录", "data": []})
        return real(request)

    sync.p115._client._transport = httpx.MockTransport(expired)
    r = sync.run(FULL)
    assert r.errors and "登入失效" in r.errors[0] and r.removed == 0
    assert strm.exists() and sync.p115.breaker.login_bad

    # 一般錯誤（不是限流、不是登入）帶著 data: [] 也一樣是錯誤
    sync.p115.breaker.reset()

    def broken(request):
        if request.url.path == "/files" and request.url.params.get("cur") == "1":
            return httpx.Response(200, json={"state": False, "errNo": 20004, "error": "参数错误", "data": []})
        return real(request)

    sync.p115._client._transport = httpx.MockTransport(broken)
    r = sync.run(FULL)
    assert r.errors and r.removed == 0 and strm.exists()


def test_json_throttle_does_not_fall_back_to_walk(tmp_path: Path):
    """限流不只 HTTP 405：JSON 裡的 errno 770004 也要停下，不能改成逐層列目錄。"""
    fake = Fake115()
    sync = make(tmp_path, fake)
    real = fake.handler

    def capped(request):
        if request.url.path == "/files/export_dir" and request.method == "POST":
            return httpx.Response(200, json={"state": False, "errno": 770004, "error": "访问上限"})
        return real(request)

    sync.p115._client._transport = httpx.MockTransport(capped)
    r = sync.run(FULL)
    assert sync.p115.breaker.tripped and r.errors and "限流" in r.errors[0]
    assert not [c for c in fake.calls if c[0] == "/files" and c[1].get("cur") == "1"]


def test_throttled_tree_download_stops_retrying(tmp_path: Path):
    fake = Fake115()
    sync = make(tmp_path, fake)
    real = fake.handler
    cdn = []

    def throttled(request):
        if request.url.host == "cdn.115.test":
            cdn.append(1)
            return httpx.Response(405)
        return real(request)

    sync.p115._client._transport = httpx.MockTransport(throttled)
    r = sync.run(FULL)
    assert len(cdn) == 1 and r.errors and "限流" in r.errors[0]
    assert not [c for c in fake.calls if c[0] == "/files" and c[1].get("cur") == "1"]


def test_non_ascii_cookie_is_rejected():
    svc = service(lambda r: httpx.Response(200, json={"state": True}))
    with pytest.raises(P115Error, match="重新複製"):
        svc.set_cookies("UID=1; CID=中文")
    with pytest.raises(P115Error):
        svc.set_cookies("UID=1\nCID=2")
    assert svc.cookies == "UID=1"


def test_failed_link_fetch_releases_its_lock():
    svc = service(lambda r: httpx.Response(405))
    with pytest.raises(P115Error):
        svc.download_url("a" * 17, "UA")
    assert svc._key_locks == {}


def test_self_pointing_strm_fails_fast_when_115_fails(tmp_path: Path):
    """自己產生的 strm 指回本機；115 取不到直鏈時不能再轉回自己（會再向 115 失敗一次），要直接回錯。"""
    from embyserver.config import RedirectConfig
    from embyserver.redirect import Redirector

    svc = service(lambda r: httpx.Response(200, json={"state": False, "errno": 20004, "error": "参数错误"}))
    rd = Redirector(RedirectConfig(), svc)
    own = tmp_path / "own.strm"
    own.write_text(f"http://mi302.local:8096/d/{'a' * 17}.mkv")
    item = {"id": 1, "is_strm": 1, "path": str(own), "container": "mkv"}
    with pytest.raises(P115Error):
        rd.final_url(item, {"host": "mi302.local:8096", "user-agent": "Infuse"})
    # 別台工具的 strm（例如 P115StrmHelper）：115 失敗時仍改用原網址
    other = tmp_path / "other.strm"
    other.write_text(f"http://nas.local:3000/api/v1/plugin/p115strmhelper/redirect_url?pickcode={'a' * 17}")
    item["path"] = str(other)
    assert rd.final_url(item, {"host": "mi302.local:8096", "user-agent": "Infuse"}).startswith("http://nas.local:3000/")
