"""集數定位模板、瀏覽 115、刪除劇的集和整部劇、舊 nfo（假 115 + 假 MoviePilot）；整理本身見 test_organize115。"""

import json
import re
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.p115 import P115Service
from embyserver.reorganize import episode_template, template_episode
from embyserver.strm_sync import INCREMENTAL

from fakes import T0, Fake115

SHOW = "/影視/劇集/中国新说唱 (2017)"


def test_episode_template_and_matching():
    cases = {
        "10.潘玮柏战队面临团危机-蓝光4K.mp4": ("{ep}.{a}", 10),
        "03-比赛惊现死亡之组-蓝光4K.mp4": ("{ep}-{a}", 3),
        "01 嘻哈首战.mp4": ("{ep} {a}", 1),
        "07.mp4": ("{ep}.{a}", 7),
        "某剧 第10集 预告.mp4": ("{b}第{ep}集{a}", 10),
        "某剧 第 10 集.mp4": ("{b}第 {ep} {a}", 10),
    }
    for name, (template, ep) in cases.items():
        assert episode_template(name) == template, name
        assert template_episode(template, name) == ep, name
    # MoviePilot 自己認得、或模板取不出來的：不給模板
    assert episode_template("Show.S01E02.mkv") is None
    assert episode_template("某剧 第十二集.mp4") is None
    assert episode_template("沒有集號.mp4") is None
    # 模板對不上、或 {ep} 取出來的不是集號
    assert template_episode("{ep}.{a}", "特辑.mp4") is None
    assert template_episode("{ep}-{a}", "10.潘玮柏.mp4") is None
    assert template_episode("{ep", "10.mp4") is None and template_episode("{ep}{ep}", "10.mp4") is None
    assert template_episode("{{x}}{ep}.{a}", "{x}05.mp4") == 5


class FakeMP:
    """MoviePilot：只接受帳號登入；預覽照模板算集號，執行時在假 115 上搬檔案。"""

    def __init__(self, fake115: Fake115):
        self.fake115 = fake115
        self.calls = []
        self.outside = set()  # 這些檔名預覽時放到同步目錄外面
        self.wrong_ep = {}  # 檔名 → 故意認錯的集號
        self.version = "v3.0.9"

    def episode(self, item, template):
        if template:
            return template_episode(template, item["name"])
        m = re.search(r"EP(\d+)", item["name"])
        return int(m.group(1)) if m else None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/login/access-token":
            return httpx.Response(200, json={"access_token": "jwt"})
        if request.headers.get("Authorization") != "Bearer jwt":
            return httpx.Response(403, json={"detail": "需要登入"})
        body = json.loads(request.content or b"{}")
        self.calls.append((path, body))
        if path == "/api/v1/system/env":
            return httpx.Response(200, json={"success": True, "data": {"VERSION": self.version}})
        if path == "/api/v1/transfer/episode-format/recommend":
            return httpx.Response(200, json={"success": False, "message": "样本不足"})
        if path != "/api/v1/transfer/manual":
            return httpx.Response(404)
        items = []
        for fi in body["fileitems"]:
            if body.get("type_name") == "电影":  # 電影：放到電影資料夾（「放外面」的放到同步目錄外）
                root = "/别处" if fi["name"] in self.outside else "/影視/電影"
                target = f"{root}/{fi['basename']}/{fi['name']}"
                items.append({"source": fi["path"], "target": target, "success": True, "type": "电影"})
                if not body["preview"]:
                    items[-1]["state"] = "completed"
                continue
            ep = self.wrong_ep.get(fi["name"]) or self.episode(fi, body.get("episode_format"))
            if not ep:
                items.append({"source": fi["path"], "success": False, "message": "无法识别集数", "state": "failed"})
                continue
            root = "/别处" if fi["name"] in self.outside else SHOW
            target = f"{root}/Season 1/中国新说唱 - S01E{ep:02d}.mp4"
            item = {"source": fi["path"], "target": target, "success": True, "episode": ep, "season": 1}
            if not body["preview"]:
                self.move(int(fi["fileid"]), target)
                item["state"] = "completed"
            items.append(item)
        return httpx.Response(200, json={"success": True, "data": {"items": items}})

    def move(self, fid, target):
        f = self.fake115
        if 108 not in f.dirs:
            f.dirs[108] = ("Season 1", 106)
            f.event(17, 108, is_dir=True)
        f.file(fid).update(cid=108, n=target.rsplit("/", 1)[1])
        f.event(6, fid)


def build(tmp_path: Path, login: bool = True):
    fake = Fake115()
    fake.dirs.update({106: ("中国新说唱 (2017)", 102), 107: ("Season 01", 106)})
    names = ["10.潘玮柏战队面临团危机-蓝光4K.mp4", "03-比赛惊现死亡之组-蓝光4K.mp4", "01-嘻哈首战-蓝光4K.mp4",
             "特辑.mp4", "中国新说唱 EP05.mp4"]
    for i, n in enumerate(names):
        fake.files.append({"fid": 20 + i, "cid": 107, "n": n, "pc": chr(ord("m") + i) * 17, "s": 900_000_000,
                           "te": T0 + 100 + i})
    fake.event(2, 2)  # 之前的舊事件：第一次同步記下它，之後只看新的
    media = tmp_path / "media"
    mp_cfg = {"url": "http://mp.test", "scrape_after_sync": False, "fill_after_full_sync": False}
    if login:
        mp_cfg.update(username="admin", password="pw")
    else:
        mp_cfg.update(api_token="t")
    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")},
        "users": [{"name": "admin", "password": "pw", "admin": True}],
        "libraries": [{"name": "劇集", "type": "tvshows", "paths": [str(media / "劇集")]}],
        "moviepilot": mp_cfg,
        "p115": {"cookies": "UID=1", "strm": {"tasks": [{"remote": "/影視", "local": str(media)}], "request_delay": 0,
                                               "scan_after_sync": False, "delete_stale": True,
                                               "download_metadata": False}},
    }), scan_on_start=False)
    svc = P115Service(app.state.db, initial_cookies="UID=1", transport=httpx.MockTransport(fake.handler))
    for holder in (app.state, app.state.strm_sync, app.state.dupes, app.state.prober, app.state.redirector):
        holder.p115 = svc
    svc.download_url = lambda pc, ua="": f"https://cdn.115.test/{pc}"
    svc.export_poll = 0
    mp = FakeMP(fake)
    app.state.moviepilot._transport = httpx.MockTransport(mp.handler)
    app.state.reorganizer.sync_delay = 0
    assert app.state.strm_sync.run(INCREMENTAL).fell_back_to_full
    show = media / "劇集" / "中国新说唱 (2017)"
    (show / "tvshow.nfo").write_text("<tvshow><title>中国新说唱</title><uniqueid type='tmdb'>103863</uniqueid>"
                                     "<season>-1</season><episode>-1</episode></tvshow>", encoding="utf-8")
    for n in names[:4]:  # 刮削時沒認出集號的 nfo
        (show / "Season 01" / n).with_suffix(".nfo").write_text(
            "<episodedetails><season>-1</season><episode>-1</episode></episodedetails>", encoding="utf-8")
    app.state.scanner.scan_all()
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    return app, fake, mp, media, c, h


def test_browse_115_lists_folders(tmp_path: Path):
    app, fake, mp, media, c, h = build(tmp_path)
    # 瀏覽：用路徑打開、用資料夾 id 往下走；影片附上在媒體庫裡被認成什麼
    r = c.get("/web/api/115/files", params={"path": "/影視/劇集"}, headers=h).json()
    assert r["cid"] == 102 and r["sync_root"] == "/影視" and [d["name"] for d in r["dirs"]] == ["Dark", "中国新说唱 (2017)"]
    r = c.get("/web/api/115/files", params={"cid": 107, "path": SHOW + "/Season 01"}, headers=h).json()
    files = {f["name"]: f for f in r["files"]}
    ten = files["10.潘玮柏战队面临团危机-蓝光4K.mp4"]
    assert ten["video"] and (ten["lib"]["type"], ten["lib"]["season"], ten["lib"]["episode"], ten["lib"]["ep_from"]) == \
        ("Episode", 1, 10, "name") and ten["lib"]["series"] == "中国新说唱"
    root = c.get("/web/api/115/files", params={"path": "/"}, headers=h).json()
    assert root["cid"] == 0 and root["sync_root"] is None and [d["name"] for d in root["dirs"]] == ["影視"]
    assert c.get("/web/api/115/files", params={"path": "/沒有這個"}, headers=h).status_code == 400
    assert c.get("/web/api/115/files", params={"path": "/"}).status_code == 401


def test_folder_mode_only_drops_nfo_with_negative_numbers(tmp_path: Path):
    from embyserver.reorganize import Reorganizer

    movie, bad, ep = tmp_path / "Up (2009).strm", tmp_path / "10.xx.strm", tmp_path / "S01E02.strm"
    movie.with_suffix(".nfo").write_text("<movie><title>Up</title></movie>", encoding="utf-8")
    bad.with_suffix(".nfo").write_text("<episodedetails><season>-1</season><episode>-1</episode></episodedetails>")
    ep.with_suffix(".nfo").write_text("<episodedetails><title>x</title></episodedetails>")
    for strm in (movie, bad, ep):
        Reorganizer._drop_stale_nfo(strm, strict=True)
    assert movie.with_suffix(".nfo").exists() and ep.with_suffix(".nfo").exists()
    assert not bad.with_suffix(".nfo").exists()
    Reorganizer._drop_stale_nfo(ep)  # 一季整理：沒有集號的 nfo 就刪
    assert not ep.with_suffix(".nfo").exists()


def test_delete_episodes(tmp_path: Path):
    """不想整理的直接刪：這部劇的任何一集都能刪，也能整部刪；115 回收站、本機 strm、媒體庫一起處理。
    115 的 id 有 19 位，網頁拿到的是字串（JavaScript 的數字會四捨五入），送回來照樣認得。"""
    from embyserver.strm_sync import FULL

    app, fake, mp, media, c, h = build(tmp_path)
    sid = app.state.db.one("SELECT id FROM items WHERE type='Series' AND name='中国新说唱'")["id"]
    episodes = "/web/api/115/organize/episodes"
    delete = "/web/api/115/organize/delete"
    r = c.get(episodes, params={"series": sid, "season": 1}, headers=h).json()
    # 整部劇的每一集，照季、集排；集號不對的標 problem
    assert [(f["name"], f["ep_from"], f["episode"], f["problem"]) for f in r["files"]] == [
        ("特辑", "none", None, True), ("01-嘻哈首战-蓝光4K", "name", 1, True), ("03-比赛惊现死亡之组-蓝光4K", "name", 3, True),
        ("中国新说唱 EP05", "name", 5, True), ("10.潘玮柏战队面临团危机-蓝光4K", "name", 10, True)]
    assert r["folder"] == {"cid": 106, "path": SHOW, "videos": 5}
    assert [f["problem"] for f in c.get(episodes, params={"series": sid, "season": 2}, headers=h).json()["files"]] == [False] * 5
    ids = {f["name"]: f["file_id"] for f in r["files"]}
    assert c.post(delete, json={"series_id": sid, "file_ids": [2]}, headers=h).status_code == 400  # 別部劇的檔案
    assert c.post(delete, json={"series_id": sid, "file_ids": []}, headers=h).status_code == 400
    assert c.post(delete, json={"series_id": sid, "file_ids": [ids["特辑"]]}).status_code == 401

    res = c.post(delete, json={"series_id": sid, "file_ids": [ids["特辑"]], "remove_folder": True}, headers=h).json()
    assert res == {"deleted": 1, "folder_removed": False, "note": "劇集資料夾還有 4 支影片，資料夾保留"}
    assert str(ids["特辑"]) in fake.deleted
    season = media / "劇集" / "中国新说唱 (2017)" / "Season 01"
    assert not (season / "特辑.strm").exists() and not (season / "特辑.nfo").exists()
    assert not app.state.db.one("SELECT 1 FROM items WHERE path=?", (str(season / "特辑.strm"),))

    # 整部刪掉：劇集資料夾整個移到回收站
    res = c.post(delete, json={"id": "d106"}, headers=h).json()
    assert res == {"deleted": 1, "folder_removed": True, "note": ""} and fake.deleted[-1] == "106"
    assert not (media / "劇集" / "中国新说唱 (2017)").exists()
    assert not app.state.db.one("SELECT 1 FROM items WHERE id=?", (sid,))
    assert c.post(delete, json={"id": "d106"}, headers=h).status_code == 400  # 已經沒有這部劇

    # 115 真的 id：19 位，超過 JavaScript 能精確表示的範圍
    big, big_dir = 2856394156427519845, 2856394156427519000
    fake.dirs[big_dir] = ("春晚 (2026)", 102)
    for i, n in enumerate(["开场.mp4", "零点.mp4"]):
        fake.files.append({"fid": big + i, "cid": big_dir, "n": n, "pc": f"sw{i}".ljust(17, "x"), "s": 900_000_000, "te": T0 + 200 + i})
    assert not app.state.strm_sync.run(FULL).errors
    app.state.scanner.scan_all()
    gala = app.state.db.one("SELECT id FROM items WHERE type='Series' AND name='春晚'")["id"]
    r = c.get(episodes, params={"series": gala}, headers=h).json()
    assert [f["file_id"] for f in r["files"]] == [str(big), str(big + 1)] and r["folder"]["cid"] == str(big_dir)
    units = c.get("/web/api/115/organize", params={"q": "春晚"}, headers=h).json()["items"]
    assert units[0]["id"] == f"d{big_dir}"
    res = c.post(delete, json={"series_id": gala, "file_ids": [f["file_id"] for f in r["files"]], "remove_folder": True},
                 headers=h).json()
    assert res == {"deleted": 2, "folder_removed": True, "note": ""} and fake.deleted[-3:] == [str(big), str(big + 1), str(big_dir)]
    assert not (media / "劇集" / "春晚 (2026)").exists()
    assert not app.state.db.one("SELECT 1 FROM items WHERE id=?", (gala,))


def test_big_ids_are_sent_as_strings():
    from embyserver.routes.common import js_safe

    big = 2856394156427519845
    assert js_safe({"a": [big, 5, True, {"b": -big}], "c": 2 ** 53 - 1, "d": 1.5}) == \
        {"a": [str(big), 5, True, {"b": str(-big)}], "c": 2 ** 53 - 1, "d": 1.5}


def test_delete_from_browse(tmp_path: Path):
    """瀏覽 115 裡勾的刪掉：只刪真的在這個資料夾裡的；資料夾連同裡面，本機 strm 和媒體庫跟著拿掉。"""
    app, fake, mp, media, c, h = build(tmp_path)
    delete = "/web/api/115/delete"
    assert c.post(delete, json={"parent": 102, "ids": ["103"]}).status_code == 401
    assert c.post(delete, json={"parent": 102, "ids": []}, headers=h).status_code == 400
    before = list(fake.deleted)  # 全量同步用完刪掉的目錄樹檔
    r = c.post(delete, json={"parent": 102, "ids": ["103", "1"]}, headers=h)  # 1 是電影資料夾裡的檔案，不在這裡
    assert r.status_code == 400 and "不在這個資料夾裡" in r.text and fake.deleted == before
    dark = app.state.db.one("SELECT id FROM items WHERE type='Series' AND name='Dark'")
    assert dark and (media / "劇集" / "Dark").exists()
    assert c.post(delete, json={"parent": 102, "ids": ["103"]}, headers=h).json() == {"deleted": 1, "names": ["Dark"]}
    assert fake.deleted == before + ["103"] and not (media / "劇集" / "Dark").exists()
    assert not app.state.db.one("SELECT 1 FROM items WHERE id=?", (dark["id"],))
    # 檔案
    movie = media / "電影" / "Old Movie (2001).strm"
    assert movie.exists()
    assert c.post(delete, json={"parent": 101, "ids": [1]}, headers=h).json()["deleted"] == 1
    assert "1" in fake.deleted and not movie.exists()
