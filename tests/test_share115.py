"""轉存 115 分享：看懂貼上的連結、讀分享、轉存到暫存子資料夾、等 115 做完、加進「整理 115 網盤」、停止（假 115）。
選了「整理到」交給 MoviePilot 整理的那一段在 test_organize115.py（要假 MoviePilot）。"""

import threading
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from embyserver import organize115, share115
from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.p115 import P115Service
from embyserver.share115 import folder_name, parse_links

from fakes import SHARE_ID as S, Fake115, wait

READ, START = "/web/api/115/share/read", "/web/api/115/share/start"
ITEMS = [str(S + 1), str(S + 4)]  # 分享最上層的資料夾和檔案（19 位的 id，用字串送）


def fanren():
    return {"receive_code": "ab12", "title": "凡人修仙传 第一季 4K",
            "dirs": {S + 1: ("凡人修仙传", 0), S + 2: ("Season 1", S + 1)},
            "files": [{"fid": S + 3, "cid": S + 2, "n": "凡人修仙传 S01E01.mkv", "s": 1_000_000_000},
                      {"fid": S + 4, "cid": 0, "n": "说明.txt", "s": 100}]}


def build(tmp_path: Path):
    fake = Fake115()
    fake.dirs[200] = ("待整理", 0)
    fake.shares["swabc"] = fanren()
    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")},
        "users": [{"name": "admin", "password": "pw", "admin": True}],
        "p115": {"cookies": "UID=1", "strm": {"tasks": [{"remote": "/影視", "local": str(tmp_path / "media")}],
                                               "request_delay": 0, "scan_after_sync": False}},
    }), scan_on_start=False)
    svc = P115Service(app.state.db, initial_cookies="UID=1", transport=httpx.MockTransport(fake.handler))
    for holder in (app.state, app.state.strm_sync):  # 轉存和整理都用 strm_sync.p115
        holder.p115 = svc
    share = app.state.share
    share.pace = share.poll = share.settle = 0
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    return app, fake, c, h


def one(title="凡人修仙传 第一季 4K", ids=ITEMS, code="swabc", receive_code="ab12"):
    return {"code": code, "receive_code": receive_code, "title": title, "ids": ids}


def run(app, c, h, shares, folder="/待整理"):
    r = c.post(START, json={"shares": shares, "folder": folder, "target": "", "target_path": ""}, headers=h)
    assert r.status_code == 200, r.text
    wait(lambda: not app.state.share.job.running)
    return c.get("/web/api/115/share/status", headers=h).json()


def test_parse_links_and_folder_name():
    shares, bad = parse_links(
        "https://115.com/s/swabc?password=ab12#\n"
        "链接：https://115cdn.com/s/swdef 访问码：cd34 复制这段内容\n"
        "【115】anxia.com/s/swghi 提取碼： ef56\n"
        "https://115.com/s/swjkl\n"
        "https://115.com/s/swabc 又貼了一次\n"
        "https://115.com/s/swjkl 密碼 gh78\n"
        "https://115cdn.com/s/swmno 提取码:x9y8\n"
        "看不懂的一行\n"
        "https://example.com/s/xyz")
    assert shares == [{"code": "swabc", "receive_code": "ab12"}, {"code": "swdef", "receive_code": "cd34"},
                      {"code": "swghi", "receive_code": "ef56"}, {"code": "swjkl", "receive_code": "gh78"},
                      {"code": "swmno", "receive_code": "x9y8"}]
    assert bad == ["看不懂的一行", "https://example.com/s/xyz"]
    assert folder_name(' ..凡人/修仙:传*?"<>|\x01 第一季.. ', "sw1") == "凡人修仙传 第一季"
    assert folder_name(" ... ", "sw1") == "sw1" and len(folder_name("長" * 300, "sw1")) == 100


def test_read_lists_top_level_items(tmp_path: Path, monkeypatch):
    app, fake, c, h = build(tmp_path)
    monkeypatch.setattr(share115, "SNAP_PAGE", 1)  # 一頁一項：要翻頁
    r = c.post(READ, json={"text": "https://115.com/s/swabc?password=ab12\n115cdn.com/s/swnope\n亂打的"}, headers=h).json()
    ok, gone = r["shares"]
    assert (ok["code"], ok["title"], ok["count"], ok["size"], ok["more"], ok["error"]) == \
        ("swabc", "凡人修仙传 第一季 4K", 2, 1_000_000_100, 0, "")
    assert ok["items"] == [{"id": str(S + 1), "name": "凡人修仙传", "is_dir": True, "size": 0},
                           {"id": str(S + 4), "name": "说明.txt", "is_dir": False, "size": 100}]
    assert sum(1 for p, q in fake.calls if p == "/share/snap" and q["share_code"] == "swabc") == 2
    assert "该文件分享链接不存在或已被删除" in gone["error"] and gone["items"] == []  # 115 的原文
    assert r["rejected"] == ["亂打的"]
    wrong = c.post(READ, json={"text": "https://115.com/s/swabc?password=zz99"}, headers=h).json()["shares"][0]
    assert "访问码错误" in wrong["error"]
    r = c.post(READ, json={"text": "亂打"}, headers=h)
    assert r.status_code == 400 and "看得懂的分享連結" in r.text


def test_transfer_waits_for_115_and_pins_each_share(tmp_path: Path, monkeypatch):
    app, fake, c, h = build(tmp_path)
    fake.receive_lag = 2  # 115 在背景轉存：再列兩次暫存資料夾才出現
    j = run(app, c, h, [one()])
    e = j["shares"][0]
    temp = next(cid for cid, (n, parent) in fake.dirs.items() if n == "凡人修仙传 第一季 4K" and parent == 200)
    assert (e["state"], e["path"], e["unit_id"], e["message"]) == \
        ("pinned", "/待整理/凡人修仙传 第一季 4K", f"d{temp}", "已加進「整理 115 網盤」")
    assert (j["running"], j["error"], j["folder"], j["in_sync"]) == (False, "", "/待整理", False)
    assert ("/files/add", {"pid": "200", "cname": "凡人修仙传 第一季 4K"}) in fake.forms
    assert ("/share/receive", {"share_code": "swabc", "receive_code": "ab12", "file_id": ",".join(ITEMS),
                               "cid": str(temp)}) in fake.forms
    assert any(f["n"] == "凡人修仙传 S01E01.mkv" and fake.under(temp, f["cid"]) for f in fake.files)
    assert sum(1 for p, q in fake.calls if p == "/category/get" and q["cid"] == str(temp)) == 2  # 兩次一樣才算完
    pinned = c.get("/web/api/115/organize", headers=h).json()["pinned"]
    assert [(u["id"], u["path"]) for u in pinned] == [(f"d{temp}", "/待整理/凡人修仙传 第一季 4K")]

    # 同一個分享再轉存一次：同名的子資料夾已經有了，改叫「名稱 (分享碼)」
    e = run(app, c, h, [one()])["shares"][0]
    assert (e["state"], e["path"]) == ("pinned", "/待整理/凡人修仙传 第一季 4K (swabc)")

    # 115 拒絕轉存：失敗、照原文，寫明暫存資料夾在哪
    fake.receive_error = "文件已被删除或不存在"
    e = run(app, c, h, [one("拒絕")])["shares"][0]
    assert e["state"] == "failed" and "文件已被删除或不存在" in e["message"] and "/待整理/拒絕" in e["message"]
    fake.receive_error = ""

    # 115 一直沒做完：等到上限就算失敗，說明檔案會在哪
    fake.receive_lag = None
    app.state.share.wait_max = 0.2
    e = run(app, c, h, [one("等不到")])["shares"][0]
    assert e["state"] == "failed" and "115 還在轉存" in e["message"] and "/待整理/等不到" in e["message"]
    assert not app.state.organizer.is_pinned(e["unit_id"])

    # 一次轉存超過釘選清單的上限：前面的被擠掉，寫明檔案在哪
    fake.receive_lag = 0
    fake.shares["swdef"] = {**fanren(), "receive_code": ""}
    monkeypatch.setattr(organize115, "MAX_PINNED", 1)
    first, second = run(app, c, h, [one("擠掉的"), one("留下的", code="swdef", receive_code="")])["shares"]
    assert (first["state"], first["unit_id"]) == ("pinned", "") and "釘選清單滿了" in first["message"]
    assert "/待整理/擠掉的" in first["message"] and app.state.organizer.is_pinned(second["unit_id"])


def test_start_checks_and_stop(tmp_path: Path):
    app, fake, c, h = build(tmp_path)

    def start(shares, **kw):
        return c.post(START, json={"shares": shares, "folder": "/待整理", "target": "", "target_path": "", **kw}, headers=h)

    for r, why in ((start([one()], folder="/"), "不能是最上層"), (start([one()], folder="/沒有這個"), "找不到 115 資料夾"),
                   (start([one()], target="path", target_path="/影視"), "還沒設定 MoviePilot"),
                   (start([one(ids=[1.5])]), "id 要是數字"), (start([one(ids=[])]), "沒有勾要轉存的"),
                   (start([one()], target="auto"), "「整理到」")):
        assert r.status_code == 400 and why in r.text, r.text
    assert not app.state.share.job.started  # 都沒開始

    # 停止：做完手上這一個就停，後面的不做；轉存中重新啟動會中斷，確認框要列出來
    fake.shares["swdef"] = {**fanren(), "receive_code": "", "title": "第二個"}
    share, gate, started = app.state.share, threading.Event(), threading.Event()
    real = share._one

    def slow(*a):
        started.set()
        gate.wait(5)
        return real(*a)

    share._one = slow
    assert start([one(), one("第二個", code="swdef", receive_code="")]).status_code == 200
    assert started.wait(5)
    r = start([one()])
    assert r.status_code == 400 and "已經在轉存了" in r.text
    assert "轉存分享" in c.get("/web/api/server", headers=h).json()["busy"]
    assert c.post("/web/api/115/share/stop", headers=h).json()["stopped"]
    gate.set()
    wait(lambda: not share.job.running)
    assert [e["state"] for e in share.job.shares] == ["pinned", "skipped"] and share.job.stopped
    assert "按了停止" in share.job.shares[1]["message"] and not any(f[1].get("share_code") == "swdef" for f in fake.forms)
    assert c.post("/web/api/115/share/stop", headers=h).json()["stopped"] is False
    del share._one

    # 轉存途中 115 限流熔斷：後面的不做，寫明原因；熔斷中不開始
    share._one = lambda *a: (real(*a), app.state.p115.breaker.trip("HTTP 405"))
    assert start([one("限流前"), one("限流後", code="swdef", receive_code="")]).status_code == 200
    wait(lambda: not share.job.running)
    assert [e["state"] for e in share.job.shares] == ["pinned", "skipped"] and "115 限流" in share.job.error
    assert "115 限流" in share.job.shares[1]["message"]
    r = start([one()])
    assert r.status_code == 400 and "115 限流" in r.text
    app.state.p115.breaker.reset()

    # 沒預料到的錯：停下、寫進 error，停在中間的不留在「轉存中」「等待中」
    def broken(entry, *a):
        entry["state"] = "receiving"
        raise RuntimeError("壞了")

    share._one = broken
    assert start([one("壞一"), one("壞二", code="swdef", receive_code="")]).status_code == 200
    wait(lambda: not share.job.running)
    assert [e["state"] for e in share.job.shares] == ["failed", "skipped"] and share.job.error == "RuntimeError: 壞了"
    del share._one

    # 沒用 cookie 登入：讀分享、轉存都不行
    app.state.p115.set_cookies("")
    for r in (start([one()]), c.post(READ, json={"text": "https://115.com/s/swabc"}, headers=h)):
        assert r.status_code == 400 and "掃碼登入" in r.text
