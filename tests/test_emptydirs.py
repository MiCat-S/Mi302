"""115 上的空資料夾：從目錄樹算出哪些資料夾底下沒有影音檔，到 115 上確認，刪之前再確認一次，本機跟著清掉。"""

import json
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.emptydirs import classify, find_empty
from embyserver.p115 import P115Service
from embyserver.strm_sync import FULL

from fakes import T0, Fake115


class Fake(Fake115):
    """刪資料夾時連裡面的一起拿掉；資料夾可以有修改時間（列目錄時的 te）。"""

    def __init__(self):
        super().__init__()
        self.dir_te = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rb/delete":
            form = dict(httpx.QueryParams(request.content.decode()))
            ids = {int(v) for k, v in form.items() if k.startswith("fid[")}
            gone = {d for d in self.dirs if any(self.under(i, d) for i in ids if i in self.dirs)}
            self.dirs = {c: v for c, v in self.dirs.items() if c not in gone}
            self.files = [f for f in self.files if f["cid"] in self.dirs]
        resp = super().handler(request)
        if request.url.path == "/files" and request.url.params.get("cur") != "0" and resp.status_code == 200:
            data = json.loads(resp.content)
            for item in data.get("data") or []:
                if "fid" not in item and item["cid"] in self.dir_te:
                    item["te"] = self.dir_te[item["cid"]]
            return httpx.Response(200, json=data)
        return resp


def build(tmp_path: Path):
    fake = Fake()
    fake.dirs.update({
        104: ("Old Movie Copy", 101),  # 只剩 nfo、海報
        105: ("Empty", 102),  # 完全是空的
        106: ("Show", 102), 107: ("Season 1", 106), 108: ("Season 2", 106),  # 整部劇都沒有影片
        109: ("Season 9", 103),  # 有影片的劇裡一個空的季
        110: ("Disc Movie", 101), 111: ("BDMV", 110), 112: ("STREAM", 111), 113: ("CLIPINF", 111), 114: ("CERTIFICATE", 110),
        115: ("Fresh", 102),  # 剛建好的（可能 MoviePilot 正要把影片搬進去）
        200: ("待整理", 0),
    })
    fake.dir_te[115] = int(time.time())
    fake.files += [
        {"fid": 20, "cid": 104, "n": "movie.nfo", "pc": "h" * 17, "s": 1000, "te": T0},
        {"fid": 21, "cid": 104, "n": "poster.jpg", "pc": "i" * 17, "s": 50_000, "te": T0},
        {"fid": 22, "cid": 108, "n": "notes.txt", "pc": "j" * 17, "s": 10, "te": T0},
        {"fid": 23, "cid": 112, "n": "00001.m2ts", "pc": "k" * 17, "s": 900_000_000, "te": T0},
    ]
    media = tmp_path / "media"
    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")},
        "users": [{"name": "admin", "password": "pw", "admin": True}],
        "libraries": [
            {"name": "電影", "type": "movies", "paths": [str(media / "電影")]},
            {"name": "劇集", "type": "tvshows", "paths": [str(media / "劇集")]},
        ],
        "p115": {"cookies": "UID=1", "strm": {"tasks": [{"remote": "/影視", "local": str(media)}], "request_delay": 0,
                                               "scan_after_sync": False, "download_metadata": False}},
    }), scan_on_start=False)
    svc = P115Service(app.state.db, initial_cookies="UID=1", transport=httpx.MockTransport(fake.handler))
    for holder in (app.state, app.state.strm_sync, app.state.dupes, app.state.prober, app.state.redirector):
        holder.p115 = svc
    svc.download_url = lambda pc, ua="": f"https://cdn.115.test/{pc}"
    svc.export_poll = 0
    app.state.empty_dirs.pace = 0
    app.state.strm_sync.run(FULL)
    app.state.scanner.scan_all()
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    return app, fake, media, c, h


def listings(fake):
    """一層一層列目錄的請求（不含遞迴列檔案、導出目錄樹）。"""
    return [c[1]["cid"] for c in fake.calls if c[0] == "/files" and c[1].get("cur") == "1"]


def wait_job(app):
    for _ in range(200):
        if not app.state.empty_dirs.job.running:
            return app.state.empty_dirs.job
        time.sleep(0.02)
    raise AssertionError("找空資料夾／刪空資料夾沒有結束")


EXPECTED = ["/影視/劇集/Dark/Season 9", "/影視/劇集/Empty", "/影視/劇集/Show", "/影視/電影/Old Movie Copy"]


def test_scan_and_delete_empty_folders(tmp_path: Path):
    # 什麼算空資料夾：底下沒有影音檔（音樂也算），只列最外層；藍光原盤裡的、和 BDMV 並排的不算
    tree = [
        ("電影",), ("電影", "A (2001)"), ("電影", "A (2001)", "A.mkv"),
        ("電影", "A (2001)", "Extras"), ("電影", "A (2001)", "Extras", "x.nfo"),  # 電影資料夾裡只有 nfo 的子資料夾
        ("劇集",), ("劇集", "Show"), ("劇集", "Show", "Season 1"),  # 整部劇都沒有影片：只列劇的資料夾
        ("劇集", "Show", "Season 2"), ("劇集", "Show", "Season 2", "s.nfo"),
        ("劇集", "Dark"), ("劇集", "Dark", "Dark.S01E01.mp4"), ("劇集", "Dark", "Season 9"),
        # 藍光原盤：BDMV 裡沒有影片的資料夾、和 BDMV 並排的都是原盤的一部分
        ("原盤",), ("原盤", "Film"), ("原盤", "Film", "BDMV"), ("原盤", "Film", "BDMV", "STREAM"),
        ("原盤", "Film", "BDMV", "STREAM", "00001.m2ts"), ("原盤", "Film", "BDMV", "CLIPINF"),
        ("原盤", "Film", "CERTIFICATE"), ("原盤", "Film", "ANY!"),
        ("音樂",), ("音樂", "OST"), ("音樂", "OST", "01.flac"),  # 音樂也算影音檔
        ("空的",), ("README",),
    ]
    names = {"A.mkv", "x.nfo", "s.nfo", "Dark.S01E01.mp4", "00001.m2ts", "01.flac", "README"}
    nodes = classify(tree, names)
    # 最底層的：115 列出的檔案裡有這個名稱的是檔案，沒有的是空資料夾
    assert nodes["README"] is False and nodes["空的"] is True and nodes["劇集/Dark/Season 9"] is True
    assert find_empty(nodes) == ["劇集/Dark/Season 9", "劇集/Show", "空的", "電影/A (2001)/Extras"]

    app, fake, media, c, h = build(tmp_path)
    leftover = media / "電影" / "Old Movie Copy"
    leftover.mkdir(parents=True)
    (leftover / "movie.nfo").write_text("<movie/>")  # Mi302 之前下載的
    (media / "劇集" / "Show" / "Season 1").mkdir(parents=True)

    assert c.get("/web/api/empty-dirs", headers=h).json()["default_roots"] == ["/影視"]
    fake.calls.clear()
    assert c.post("/web/api/empty-dirs/scan", json={}, headers=h).json()["started"]
    job = wait_job(app)
    # 不一個一個列空資料夾：每個上一層列一次（電影、劇集、Dark），只有子資料夾裡也有檔案的 Show 整個列一遍
    assert sorted(listings(fake)) == ["101", "102", "103", "106", "107", "108"]
    assert not job.errors and not job.notes and job.listed > 0
    s = c.get("/web/api/empty-dirs", headers=h).json()
    assert (s["count"], s["size"], s["roots"], s["skipped"]) == (4, 51_010, ["/影視"], {"recent": 1})
    r = c.get("/web/api/empty-dirs/list", headers=h).json()
    assert [d["path"] for d in r["items"]] == EXPECTED and r["total"] == 4
    copy = r["items"][3]
    assert (copy["cid"], copy["files"], copy["dirs"], copy["size"], copy["sample"]) == ("104", 2, 0, 51_000, ["movie.nfo", "poster.jpg"])
    show = r["items"][2]
    assert (show["files"], show["dirs"], show["sample"]) == (1, 2, ["Season 2/notes.txt"])
    assert c.get("/web/api/empty-dirs/list", params={"q": "劇集"}, headers=h).json()["total"] == 3

    # 只算數量：勾了的、或全選（符合搜尋的全部，取消勾的除外）
    r = c.post("/web/api/empty-dirs/delete", json={"dry_run": True, "overrides": {"104": True}}, headers=h).json()
    assert (r["started"], r["count"], r["size"]) == (False, 1, 51_000)
    r = c.post("/web/api/empty-dirs/delete", json={"dry_run": True, "all": True, "q": "劇集", "overrides": {"106": False}},
               headers=h).json()
    assert r["count"] == 2

    # MoviePilot 正在整理時不刪
    assert app.state.reorganizer.hold()
    r = c.post("/web/api/empty-dirs/delete", json={"all": True}, headers=h)
    assert r.status_code == 409 and "整理" in r.text
    app.state.reorganizer.release()

    # 掃描之後有影片搬進「Empty」：刪之前再確認時發現，不刪
    fake.files.append({"fid": 30, "cid": 105, "n": "New.S01E01.mkv", "pc": "l" * 17, "s": 900_000_000, "te": T0})
    deleted_before = len(fake.deleted)
    fake.calls.clear()
    r = c.post("/web/api/empty-dirs/delete", json={"all": True}, headers=h).json()
    assert (r["started"], r["count"]) == (True, 4)
    job = wait_job(app)
    assert not job.errors and (job.done, job.kept, job.freed) == (3, 1, 51_010)
    assert job.results == [{"path": "/影視/劇集/Empty", "why": "現在裡面有影音檔了，沒有刪"}]
    # 每個上一層重新導出一次目錄樹、列一次，不一個一個列
    assert sorted(listings(fake)) == ["101", "102", "103"]
    assert sorted(i for i in fake.deleted[deleted_before:] if not i.startswith("9")) == ["104", "106", "109"]  # 9xxx 是目錄樹檔
    assert 105 in fake.dirs and 103 in fake.dirs and 110 in fake.dirs
    # 本機對應的資料夾跟著拿掉，有影片的不動
    assert not leftover.exists() and not (media / "劇集" / "Show").exists()
    assert (media / "劇集" / "Dark" / "Dark.S01E01.strm").exists()
    # 刪掉的、不再是空的都從清單拿掉
    assert c.get("/web/api/empty-dirs", headers=h).json()["count"] == 0


def test_scan_whole_drive_and_export_failures(tmp_path: Path):
    """範圍是整個網盤時最上層的資料夾一個一個掃；導出目錄樹失敗時不改成逐層列目錄，刪除也先不刪。"""
    app, fake, media, c, h = build(tmp_path)
    app.state.strm_sync.result.running = True  # 同步也要導出目錄樹：等它做完
    r = c.post("/web/api/empty-dirs/scan", json={}, headers=h)
    assert r.status_code == 409 and "同步" in r.text
    app.state.strm_sync.result.running = False

    assert c.post("/web/api/empty-dirs/scan", json={"paths": ["/"]}, headers=h).json()["started"]
    job = wait_job(app)
    assert not job.errors and not job.notes
    s = c.get("/web/api/empty-dirs", headers=h).json()
    assert (s["roots"], s["skipped"], s["count"]) == (["/"], {"recent": 1}, 4)
    assert [d["path"] for d in c.get("/web/api/empty-dirs/list", headers=h).json()["items"]] == EXPECTED

    fake.export_ok = False  # 例如 115 正在導出別的目錄樹
    deleted_before = list(fake.deleted)
    assert c.post("/web/api/empty-dirs/delete", json={"all": True}, headers=h).json()["started"]
    job = wait_job(app)
    assert not job.errors and (job.done, job.kept) == (0, 4) and fake.deleted == deleted_before
    assert "重新確認時 115 出錯" in job.results[0]["why"]

    fake.calls.clear()
    assert c.post("/web/api/empty-dirs/scan", json={}, headers=h).json()["started"]
    job = wait_job(app)
    assert not job.errors and "導出目錄樹失敗" in job.notes[0]
    assert listings(fake) == []  # 不一層一層列
    assert c.get("/web/api/empty-dirs", headers=h).json()["count"] == 4  # 留著上次的結果
