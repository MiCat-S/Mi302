"""從阿里雲盤秒傳到 115：阿里雲盤的登入和換 token、列目錄、讀一段；115 的上傳初始化（照真的加解密）、二次驗證、
版本號、限流；整批秒傳的資料夾結構、略過的、115 沒有的、一支都沒成功時清掉空資料夾、加進整理、停止（假 115、假阿里雲盤）。"""

import hashlib
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from embyserver.aliyun import PACES, AliyunError, _Pace
from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.http_util import GuardedClient
from embyserver.p115 import P115Service, P115Throttled

from fakes import Fake115, FakeAliyun, wait

READ, START, STATUS = "/web/api/115/rapid/read", "/web/api/115/rapid/start", "/web/api/115/rapid/status"
V1, V2, V3 = bytes(range(256)), b"second episode " * 10, b"no sha1 here" * 10
SUB, BIG = "字幕：第一集".encode() * 4, b"x" * 5000
LIMIT = 1000  # 115 的單檔上限：只有 BIG 超過


def sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest().upper()


def build(tmp_path: Path, mp: bool = False):
    fake, ali = Fake115(), FakeAliyun()
    fake.dirs[200] = ("待整理", 0)
    raw = {"server": {"data_dir": str(tmp_path / "data")}, "users": [{"name": "admin", "password": "pw", "admin": True}],
           "p115": {"cookies": "UID=1", "strm": {"tasks": [{"remote": "/影視", "local": str(tmp_path / "media")}],
                                                  "request_delay": 0, "scan_after_sync": False}}}
    if mp:
        raw["moviepilot"] = {"url": "http://mp.test", "api_token": "t", "scrape_after_sync": False, "fill_after_full_sync": False}
    app = create_app(config_from_dict(raw), scan_on_start=False)
    svc = P115Service(app.state.db, initial_cookies="UID=1", transport=httpx.MockTransport(fake.handler))
    for holder in (app.state, app.state.strm_sync):  # 秒傳和整理都用 strm_sync.p115
        holder.p115 = svc
    aliyun = app.state.aliyun
    aliyun._client = GuardedClient(AliyunError, transport=httpx.MockTransport(ali))
    aliyun._paces = {k: _Pace(0) for k in PACES}
    app.state.moviepilot._transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"success": False, "message": "未识别到媒体信息"}))
    app.state.rapid.pace = 0
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    return app, fake, ali, c, h


def tree(ali: FakeAliyun) -> None:
    """/資源庫/凡人修仙传/{Season 1/{01.mkv, 01.ass, 02.mkv}, fanren.nfo, no-sha.mkv, big.mkv}"""
    ali.add("d1", "凡人修仙传")
    ali.add("d2", "Season 1", parent="d1")
    ali.add("v1", "01.mkv", parent="d2", data=V1)
    ali.add("s1", "01.ass", parent="d2", data=SUB)
    ali.add("v2", "02.mkv", parent="d2", data=V2)
    ali.add("n1", "fanren.nfo", parent="d1", data=b"<tvshow/>")
    ali.add("x1", "no-sha.mkv", parent="d1", data=V3, sha1=False)
    ali.add("b1", "big.mkv", parent="d1", data=BIG)


def login(c, h):
    r = c.post("/web/api/aliyun/token", json={"refresh_token": "rt0"}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def run(app, c, h, **body):
    body = {"source": "/資源庫/凡人修仙传", "folder": "/待整理", "target": "", "target_path": "", **body}
    r = c.post(START, json=body, headers=h)
    assert r.status_code == 200, r.text
    wait(lambda: not app.state.rapid.job.running)
    return c.get(STATUS, headers=h).json()


def children(fake: Fake115, cid: int) -> dict:
    return {n: d for d, (n, parent) in fake.dirs.items() if parent == cid}


def test_aliyun_login_lists_and_rotates_token(tmp_path: Path):
    app, fake, ali, c, h = build(tmp_path)
    tree(ali)
    db = app.state.db
    assert c.get("/web/api/aliyun/status", headers=h).json()["logged_in"] is False
    r = c.post("/web/api/aliyun/token", json={"refresh_token": "wrong"}, headers=h)
    assert r.status_code == 400 and "invalid refresh_token" in r.text and not app.state.aliyun.logged_in  # 原文、不留下
    assert login(c, h) == {"logged_in": True, "name": "小明", "drives": ["資源庫", "備份盤"], "own_client": False, "error": ""}
    assert db.get_meta("aliyun_refresh_token") == "rt1"  # 換回來的 refresh token 換新了，存回資料庫
    assert c.get("/web/api/aliyun/dirs", headers=h).json() == {"path": "/", "parent": None, "dirs": ["資源庫", "備份盤"],
                                                                "files": 0}
    d = c.get("/web/api/aliyun/dirs", params={"path": "/資源庫/凡人修仙传"}, headers=h).json()  # 一頁兩項：要翻頁
    assert (d["path"], d["parent"], d["dirs"], d["files"]) == ("/資源庫/凡人修仙传", "/資源庫", ["Season 1"], 3)
    # access token 過期：換一次再試，新的 refresh token 一樣存回去
    ali.expire_next = True
    assert c.get("/web/api/aliyun/dirs", params={"path": "/資源庫"}, headers=h).json()["dirs"] == ["凡人修仙传"]
    assert db.get_meta("aliyun_refresh_token") == "rt2"
    r = c.get("/web/api/aliyun/dirs", params={"path": "/資源庫/沒有這個"}, headers=h)
    assert r.status_code == 400 and "找不到 /資源庫/沒有這個" in r.text
    # 讀一段：長度要剛好
    aliyun = app.state.aliyun
    url = aliyun.download_url("r1", "v1")
    assert aliyun.read_range(url, 3, 9, len(V1)) == V1[3:10] and ali.ranges[-1] == "bytes=3-9"
    with pytest.raises(AliyunError, match="長度不對"):
        aliyun.read_range(url, len(V1) - 2, len(V1) + 5, len(V1))
    # 自己的 client id：向開放平台換，不經過線上 API
    app.state.config.aliyun.client_id, app.state.config.aliyun.client_secret = "cid", "sec"
    ali.client = ("cid", "sec")
    c.post("/web/api/aliyun/logout", headers=h)
    online = sum(1 for host, _ in ali.calls if host == "api.oplist.org")
    s = c.post("/web/api/aliyun/token", json={"refresh_token": ali.refresh}, headers=h).json()
    assert s["own_client"] and sum(1 for host, _ in ali.calls if host == "api.oplist.org") == online
    assert ("openapi.alipan.com", "/oauth/access_token") in ali.calls
    # 登入失敗：換回原本的 refresh token
    before = db.get_meta("aliyun_refresh_token")
    r = c.post("/web/api/aliyun/token", json={"refresh_token": "bad"}, headers=h)
    assert r.status_code == 400 and "refresh token 不對" in r.text and db.get_meta("aliyun_refresh_token") == before
    assert c.post("/web/api/aliyun/logout", headers=h).json()["logged_in"] is False
    assert db.get_meta("aliyun_refresh_token") == ""


def test_rapid_upload_statuses(tmp_path: Path):
    """上傳初始化照真的加解密：status 2 成功、7 二次驗證、1 115 沒有、4 版本號舊了、HTTP 405 熔斷。"""
    app, fake, ali, c, h = build(tmp_path)
    rp = app.state.rapid
    fake.known[sha(V1)] = V1
    reads = []

    def reader(start, end):
        reads.append((start, end))
        return V1[start:end + 1]

    r = rp.rapid_upload("a.mkv", len(V1), sha(V1).lower(), 200, reader)
    assert r["state"] == "ok" and r["pickcode"] and not reads
    init = fake.inits[-1]
    assert init["ua"] == "Mozilla/5.0 115Browser/36.0.1" and init["params"]["k_ec"]
    form = init["form"]
    assert (form["fileid"], form["filesize"], form["target"], form["userid"], form["userkey"], form["appversion"]) == \
        (sha(V1), str(len(V1)), "U_1_200", "1", "UK", "36.0.1")
    assert any(f["n"] == "a.mkv" and f["cid"] == 200 for f in fake.files)

    # 二次驗證：讀它要的那一段，SHA1（大寫）當 sign_val 再送
    fake.verify = True
    assert rp.rapid_upload("b.mkv", len(V1), sha(V1), 200, reader)["state"] == "ok" and reads == [(3, 9)]
    assert (fake.inits[-1]["form"]["sign_key"], fake.inits[-1]["form"]["sign_val"]) == ("SK", sha(V1[3:10]))
    r = rp.rapid_upload("c.mkv", len(V1), sha(V1), 200, lambda s, e: b"x" * (e - s + 1))
    assert r == {"state": "failed", "message": "115：sig invalid", "pickcode": ""}  # 115 的原文

    assert rp.rapid_upload("d.mkv", 10, "A" * 40, 200, reader)["state"] == "missing"

    # 版本號舊了：不用快取重讀一次再送
    fake.appver = fake.appver_served = "36.0.2"
    assert rp.rapid_upload("e.mkv", len(V1), sha(V1), 200, reader)["state"] == "ok"
    assert [i["ua"] for i in fake.inits[-3:-1]] == ["Mozilla/5.0 115Browser/36.0.1", "Mozilla/5.0 115Browser/36.0.2"]

    fake.init_http = 405
    with pytest.raises(P115Throttled):
        rp.rapid_upload("f.mkv", len(V1), sha(V1), 200, reader)
    assert app.state.p115.breaker.tripped


def test_rapid_folder_keeps_structure_and_pins(tmp_path: Path):
    app, fake, ali, c, h = build(tmp_path)
    tree(ali)
    login(c, h)
    fake.known = {sha(V1): V1, sha(SUB): SUB, sha(BIG): BIG}
    fake.size_limit = LIMIT
    fake.verify = True
    fake.dirs[300] = ("凡人修仙传", 200)  # 同名的已經有了：建「凡人修仙传 (2)」
    r = c.post(READ, json={"source": "/資源庫/凡人修仙传"}, headers=h).json()
    assert (r["total"], r["videos"], r["subtitles"], r["ignored"], r["no_sha1"], r["too_big"], r["size_limit"]) == \
        (5, 4, 1, 1, 1, 1, LIMIT)
    assert [i["path"] for i in r["items"]] == ["big.mkv", "no-sha.mkv", "Season 1/01.ass", "Season 1/01.mkv", "Season 1/02.mkv"]
    lists = sum(1 for _, p in ali.calls if p.endswith("openFile/list"))
    j = run(app, c, h)
    assert sum(1 for _, p in ali.calls if p.endswith("openFile/list")) == lists  # 剛讀過的不重列
    assert (j["error"], j["folder"], j["total"], j["done"]) == ("", "/待整理/凡人修仙传 (2)", 5, 5)
    assert {x["path"]: x["state"] for x in j["results"]} == {
        "big.mkv": "skipped", "no-sha.mkv": "skipped", "Season 1/01.ass": "ok", "Season 1/01.mkv": "ok",
        "Season 1/02.mkv": "missing"}
    assert (j["ok"], j["missing"], j["failed"], j["skipped"], j["ignored"]) == (2, 1, 0, 2, 1)
    root = children(fake, 200)["凡人修仙传 (2)"]
    season = children(fake, root)["Season 1"]
    assert sorted(f["n"] for f in fake.files if f["cid"] == season) == ["01.ass", "01.mkv"]  # 子資料夾照原樣
    assert (j["unit_id"], j["note"]) == (f"d{root}", "已加進「整理 115 網盤」") and app.state.organizer.is_pinned(j["unit_id"])
    assert len(ali.ranges) == 2  # 兩支都要二次驗證，各讀一段

    # 只有一支檔案：資料夾名稱用去掉副檔名的檔名
    j = run(app, c, h, source="/資源庫/凡人修仙传/Season 1/01.mkv", media_only=False)
    assert (j["folder"], j["ok"]) == ("/待整理/01", 1)
    # 超過上限、不是要的：讀取時就說
    r = c.post(READ, json={"source": "/資源庫/凡人修仙传", "media_only": False}, headers=h).json()
    assert (r["total"], r["others"], r["ignored"]) == (6, 1, 0)


def test_rapid_checks_cleanup_organize_and_stop(tmp_path: Path, monkeypatch):
    app, fake, ali, c, h = build(tmp_path, mp=True)
    tree(ali)
    for body, why in (({}, "還沒登入阿里雲盤"),):
        r = c.post(START, json={"source": "/資源庫/凡人修仙传", "folder": "/待整理", **body}, headers=h)
        assert r.status_code == 400 and why in r.text
    login(c, h)
    for body, why in (({"folder": "/"}, "不能是最上層"), ({"folder": "/沒有"}, "找不到 115 資料夾"),
                      ({"source": "/"}, "阿里雲盤上的一個資料夾"), ({"target": "auto"}, "「整理到」"),
                      ({"target": "path", "target_path": ""}, "整理到哪個 115 資料夾")):
        r = c.post(START, json={"source": "/資源庫/凡人修仙传", "folder": "/待整理", **body}, headers=h)
        assert r.status_code == 400 and why in r.text, (body, r.text)

    # 115 上一支都沒有：建好的空資料夾（含子資料夾）移到回收站、寫刪除紀錄
    fake.size_limit = LIMIT
    j = run(app, c, h)
    assert (j["ok"], j["missing"], j["skipped"], j["unit_id"]) == (0, 3, 2, "") and "已經移到 115 回收站" in j["note"]
    root = children(fake, 200)["凡人修仙传"]
    assert str(root) in fake.deleted
    log = c.get("/web/api/deleted", params={"source": "rapid"}, headers=h).json()
    assert [(i["path"], i["source_name"]) for i in log["items"]] == [("/待整理/凡人修仙传", "秒傳沒成功的空資料夾")]

    # 選了整理到：交給 organize_units_in_background
    calls = []
    monkeypatch.setattr(app.state.organizer, "organize_units_in_background",
                        lambda ids, target, path, cleanup=True: calls.append((ids, target, path, cleanup)) or {"running": True})
    fake.known = {sha(V1): V1}
    j = run(app, c, h, target="path", target_path="/影視/劇集")
    assert calls == [([j["unit_id"]], "path", "/影視/劇集", True)] and j["organizing"] and "已交給 MoviePilot" in j["note"]

    # 停止：做完手上這一支就停，秒傳好的加進整理、不交給 MoviePilot；秒傳中重新啟動會中斷
    rp, gate, started = app.state.rapid, threading.Event(), threading.Event()
    real = rp.rapid_upload

    def slow(*a):
        started.set()
        gate.wait(5)
        return real(*a)

    rp.rapid_upload = slow
    calls.clear()
    fake.known[sha(SUB)] = SUB
    assert c.post(START, json={"source": "/資源庫/凡人修仙传", "folder": "/待整理", "target": "path",
                               "target_path": "/影視/劇集"}, headers=h).status_code == 200
    assert started.wait(5)
    assert "秒傳" in c.get("/web/api/server", headers=h).json()["busy"]
    assert c.post(START, json={"source": "/資源庫/凡人修仙传", "folder": "/待整理"}, headers=h).status_code == 400
    assert c.post("/web/api/115/rapid/stop", headers=h).json()["stopped"]
    gate.set()
    wait(lambda: not rp.job.running)
    j = rp.job
    assert j.stopped and j.done == 3 and j.ok == 1 and not calls and "按了停止" in j.note  # 略過的兩支先記，第一支秒傳
    del rp.rapid_upload

    # 115 限流：整批停下，不加進整理，寫明秒傳好的在哪
    fake.init_http = 405
    j = run(app, c, h)
    assert "115 限流" in j["error"] and j["unit_id"] == "" and j["folder"] in j["note"]
