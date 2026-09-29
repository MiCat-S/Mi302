"""集號不對的劇交給 MoviePilot 整理：模板、清單、計畫、預覽、執行（假 115 + 假 MoviePilot）。"""

import json
import re
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.p115 import P115Service
from embyserver.reorganize import episode_template, template_episode
from embyserver.strm_sync import INCREMENTAL

from test_incremental import T0, Fake115

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


def wait(cond):
    for _ in range(300):
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("等不到")


def test_list_plan_preview_execute(tmp_path: Path):
    app, fake, mp, media, c, h = build(tmp_path)
    db = app.state.db
    src = {r["name"]: r["ep_from"] for r in db.query("SELECT i.path AS name, i.ep_from FROM items i WHERE type='Episode'")}
    assert sorted(src.values()) == ["name", "name", "name", "name", "none", "sxe"]

    r = c.get("/web/api/moviepilot/reorganize", headers=h).json()
    assert r["ready"] == {"moviepilot": True, "login": True, "p115": True}
    assert r["total"] == 1  # Dark.S01E01 是標準檔名，不列
    row = r["items"][0]
    assert (row["name"], row["season"], row["episodes"], row["unknown"], row["season_total"]) == ("中国新说唱", 1, 5, 1, 5)
    assert c.get("/web/api/moviepilot/reorganize", params={"q": "zgxsc"}, headers=h).json()["total"] == 1

    plan = c.get("/web/api/moviepilot/reorganize/plan", params={"series": row["series_id"], "season": 1}, headers=h).json()
    assert plan["tmdbid"] == "103863" and plan["remote_series"] == SHOW and plan["remote_parent"] == "/影視/劇集"
    groups = {g["key"]: g for g in plan["groups"]}
    assert set(groups) == {"{ep}.{a}", "{ep}-{a}", "native", "unknown"}
    assert [f["episode"] for f in groups["{ep}-{a}"]["files"]] == [1, 3]
    assert groups["{ep}.{a}"]["source"] == "mi302" and groups["native"]["template"] == ""
    assert not groups["unknown"]["enabled"] and "都認不出" in groups["unknown"]["note"]  # MoviePilot 也推薦不出來
    recommend = [b for p, b in mp.calls if p.endswith("/recommend")][0]
    assert recommend["fileitems"][0]["fileid"] == "23" and recommend["fileitems"][0]["storage"] == "u115"

    # 預覽：一個檔案被 MoviePilot 放到同步目錄外、一個集號和檔名不一樣、一個模板被改成對不上
    mp.outside.add("01-嘻哈首战-蓝光4K.mp4")
    mp.wrong_ep["03-比赛惊现死亡之组-蓝光4K.mp4"] = 4
    body = {"series_id": row["series_id"], "season": 1, "tmdbid": "103863", "target": "auto", "scrape": True,
            "groups": [{"key": g["key"], "template": g["template"], "enabled": g["enabled"]} for g in plan["groups"]]}
    mp.calls.clear()
    pv = c.post("/web/api/moviepilot/reorganize/preview", json=body, headers=h).json()
    items = {i["name"]: i for i in pv["items"]}
    assert pv["summary"] == {"total": 4, "ok": 3, "failed": 1, "warnings": 1} and pv["token"]
    assert not items["01-嘻哈首战-蓝光4K.mp4"]["ok"] and "同步目錄" in items["01-嘻哈首战-蓝光4K.mp4"]["message"]
    assert items["03-比赛惊现死亡之组-蓝光4K.mp4"]["warnings"] == ["MoviePilot 認成第 4 集，檔名看起來是第 3 集"]
    assert items["10.潘玮柏战队面临团危机-蓝光4K.mp4"]["target"] == SHOW + "/Season 1/中国新说唱 - S01E10.mp4"
    sent = [b for p, b in mp.calls if p == "/api/v1/transfer/manual"]
    assert all(b["preview"] and b["media_id"] == "103863" and b["season"] == 1 and b["transfer_type"] == "move"
               and "target_path" not in b for b in sent)
    assert {b.get("episode_format") for b in sent} == {"{ep}.{a}", "{ep}-{a}", None}
    first = next(b for b in sent if b.get("episode_format") == "{ep}.{a}")["fileitems"][0]
    assert first == {"storage": "u115", "type": "file", "path": SHOW + "/Season 01/10.潘玮柏战队面临团危机-蓝光4K.mp4",
                     "name": "10.潘玮柏战队面临团危机-蓝光4K.mp4", "basename": "10.潘玮柏战队面临团危机-蓝光4K",
                     "extension": "mp4", "size": 900_000_000, "fileid": "20", "parent_fileid": "107",
                     "pickcode": "m" * 17}

    # 模板改成對不上的：那個檔案不送，直接標失敗
    bad = json.loads(json.dumps(body))
    bad["groups"][0]["template"] = "{ep}_{a}"
    bad_items = c.post("/web/api/moviepilot/reorganize/preview", json=bad, headers=h).json()["items"]
    assert any("對不上" in i["message"] for i in bad_items)
    # 放回現在的分類資料夾：帶上目標資料夾，不另加類型、類別資料夾
    mp.calls.clear()
    c.post("/web/api/moviepilot/reorganize/preview", json={**body, "target": "parent"}, headers=h)
    parent_call = [b for p, b in mp.calls if p == "/api/v1/transfer/manual"][0]
    assert (parent_call["target_path"], parent_call["target_storage"], parent_call["library_type_folder"]) == \
        ("/影視/劇集", "u115", False)
    # 不存在的預覽代碼
    assert c.post("/web/api/moviepilot/reorganize/execute", json={"token": "nope"}, headers=h).status_code == 400

    # 執行：只送預覽成功的三個
    mp.calls.clear()
    r = c.post("/web/api/moviepilot/reorganize/execute", json={"token": pv["token"]}, headers=h)
    assert r.status_code == 200
    wait(lambda: not app.state.reorganizer.job.running)
    job = app.state.reorganizer.job
    assert (job.total, job.done, job.failed, job.errors, job.synced) == (3, 3, 0, [], "started")
    sent = [b for p, b in mp.calls if p == "/api/v1/transfer/manual"]
    assert all(not b["preview"] for b in sent)
    assert sorted(f["name"] for b in sent for f in b["fileitems"]) == [
        "03-比赛惊现死亡之组-蓝光4K.mp4", "10.潘玮柏战队面临团危机-蓝光4K.mp4", "中国新说唱 EP05.mp4"]
    wait(lambda: not app.state.strm_sync.result.running)
    new = media / "劇集" / "中国新说唱 (2017)" / "Season 1"
    assert sorted(p.name for p in new.iterdir()) == ["中国新说唱 - S01E04.strm", "中国新说唱 - S01E05.strm",
                                                    "中国新说唱 - S01E10.strm"]  # 寫著 -1 的舊 nfo 沒有跟過來
    old = media / "劇集" / "中国新说唱 (2017)" / "Season 01"
    assert sorted(p.name for p in old.iterdir()) == ["01-嘻哈首战-蓝光4K.nfo", "01-嘻哈首战-蓝光4K.strm",
                                                    "特辑.nfo", "特辑.strm"]  # 沒送的不動
    # 重新掃描後，改成標準檔名的集不再列；沒送的兩集還在
    app.state.scanner.scan_all()
    left = c.get("/web/api/moviepilot/reorganize", headers=h).json()["items"]
    assert [(i["episodes"], i["unknown"]) for i in left] == [(2, 1)]
    # 同一個預覽代碼只能用一次
    assert c.post("/web/api/moviepilot/reorganize/execute", json={"token": pv["token"]}, headers=h).status_code == 400


def test_old_moviepilot_is_never_asked_to_preview(tmp_path: Path):
    """v2.11.1-1 以前的 MoviePilot 不認 preview，會直接整理：版本太舊或查不到都不送。"""
    from embyserver.moviepilot import parse_version

    assert parse_version("v3.0.9") == (3, 0, 9, 0) and parse_version("v2.11.1-1") == (2, 11, 1, 1)
    assert parse_version("dev") is None
    app, fake, mp, media, c, h = build(tmp_path)
    row = c.get("/web/api/moviepilot/reorganize", headers=h).json()["items"][0]
    plan = c.get("/web/api/moviepilot/reorganize/plan", params={"series": row["series_id"], "season": 1}, headers=h).json()
    body = {"series_id": row["series_id"], "season": 1, "tmdbid": "103863",
            "groups": [{"key": g["key"], "template": g["template"], "enabled": True} for g in plan["groups"]]}
    for version in ("v2.11.1", "", "dev"):
        mp.version, mp.calls = version, []
        r = c.post("/web/api/moviepilot/reorganize/preview", json=body, headers=h)
        assert r.status_code == 400 and ("太舊" in r.text), r.text
        assert not [p for p, _ in mp.calls if p == "/api/v1/transfer/manual"]
    mp.version = "v2.11.1-1"
    assert c.post("/web/api/moviepilot/reorganize/preview", json=body, headers=h).status_code == 200


def test_preview_needs_moviepilot_login(tmp_path: Path):
    app, fake, mp, media, c, h = build(tmp_path, login=False)
    r = c.get("/web/api/moviepilot/reorganize", headers=h).json()
    assert r["ready"]["login"] is False
    body = {"series_id": r["items"][0]["series_id"], "season": 1, "tmdbid": "1", "groups": []}
    resp = c.post("/web/api/moviepilot/reorganize/preview", json=body, headers=h)
    assert resp.status_code == 400 and "帳號" in resp.text


def test_browse_115_and_reorganize_a_folder(tmp_path: Path):
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

    # 整理一整部劇的資料夾：類型、TMDB 編號、季都從媒體庫猜好
    plan = c.get("/web/api/moviepilot/reorganize/folder", params={"cid": 106, "path": SHOW}, headers=h).json()
    assert (plan["mode"], plan["type"], plan["tmdbid"], plan["season"], plan["in_sync"]) == ("folder", "tv", "103863", 1, 5)
    groups = {g["key"]: g for g in plan["groups"]}
    assert groups["unknown"]["enabled"] and "電影" in groups["unknown"]["note"]  # 資料夾模式：認不出集號的照樣送
    assert c.get("/web/api/moviepilot/reorganize/folder", params={"cid": 0, "path": "/"}, headers=h).status_code == 400

    mp.calls.clear()
    body = {"plan_id": plan["plan_id"], "tmdbid": "", "type": "tv", "season": 1, "target": "path",
            "target_path": "/影視/劇集", "scrape": False,
            "groups": [{"key": g["key"], "template": g["template"], "enabled": g["enabled"]} for g in plan["groups"]]}
    pv = c.post("/web/api/moviepilot/reorganize/preview", json=body, headers=h).json()
    sent = [b for p_, b in mp.calls if p_ == "/api/v1/transfer/manual"]
    assert all(b["type_name"] == "电视剧" and b["season"] == 1 and b["target_path"] == "/影視/劇集"
               and "media_id" not in b and not b["scrape"] for b in sent)  # 沒填 TMDB 編號：讓 MoviePilot 自己認
    assert pv["summary"]["ok"] == 4 and pv["token"]  # 「特辑」認不出集號：MoviePilot 也失敗
    # 執行：寫著 -1 的舊 nfo 刪掉
    assert c.post("/web/api/moviepilot/reorganize/execute", json={"token": pv["token"]}, headers=h).status_code == 200
    wait(lambda: not app.state.reorganizer.job.running)
    assert app.state.reorganizer.job.done == 4
    assert not (media / "劇集" / "中国新说唱 (2017)" / "Season 01" / "10.潘玮柏战队面临团危机-蓝光4K.nfo").exists()


def test_reorganize_movie_folder_outside_sync(tmp_path: Path):
    """同步目錄外的「待整理」：類型選電影、不送集數定位；新位置在同步目錄外只提醒，不擋。"""
    app, fake, mp, media, c, h = build(tmp_path)
    fake.dirs[200] = ("待整理", 0)
    for i, n in enumerate(["Up (2009).mkv", "Heat (1995).mkv"]):
        fake.files.append({"fid": 60 + i, "cid": 200, "n": n, "pc": chr(ord("p") + i) * 17, "s": 900_000_000, "te": T0})
    plan = c.get("/web/api/moviepilot/reorganize/folder", params={"cid": 200, "path": "/待整理"}, headers=h).json()
    assert (plan["type"], plan["tmdbid"], plan["season"], plan["in_sync"]) == ("auto", "", None, 0)
    mp.outside.add("Heat (1995).mkv")
    mp.calls.clear()
    body = {"plan_id": plan["plan_id"], "type": "movie", "target": "auto", "scrape": True,
            "groups": [{"key": g["key"], "template": "{ep}.{a}", "enabled": True} for g in plan["groups"]]}
    pv = c.post("/web/api/moviepilot/reorganize/preview", json=body, headers=h).json()
    sent = [b for p_, b in mp.calls if p_ == "/api/v1/transfer/manual"]
    assert sent and all(b["type_name"] == "电影" and "episode_format" not in b and "season" not in b for b in sent)
    items = {i["name"]: i for i in pv["items"]}
    assert items["Up (2009).mkv"]["ok"] and not items["Up (2009).mkv"]["warnings"]
    assert items["Heat (1995).mkv"]["ok"] and "不會替它產生 strm" in items["Heat (1995).mkv"]["warnings"][0]


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


def test_delete_episodes_with_wrong_numbers(tmp_path: Path):
    """不想整理的直接刪：這部劇的任何一集都能刪，也能整部刪；115 回收站、本機 strm、媒體庫一起處理。
    115 的 id 有 19 位，網頁拿到的是字串（JavaScript 的數字會四捨五入），送回來照樣認得。"""
    from embyserver.strm_sync import FULL

    app, fake, mp, media, c, h = build(tmp_path)
    sid = app.state.db.one("SELECT id FROM items WHERE type='Series' AND name='中国新说唱'")["id"]
    files = "/web/api/moviepilot/reorganize/files"
    delete = "/web/api/moviepilot/reorganize/delete"
    r = c.get(files, params={"series": sid, "season": 1}, headers=h).json()
    # 整部劇的每一集，照季、集排；這一季集號不對的標 problem
    assert [(f["name"], f["ep_from"], f["episode"], f["problem"]) for f in r["files"]] == [
        ("特辑", "none", None, True), ("01-嘻哈首战-蓝光4K", "name", 1, True), ("03-比赛惊现死亡之组-蓝光4K", "name", 3, True),
        ("中国新说唱 EP05", "name", 5, True), ("10.潘玮柏战队面临团危机-蓝光4K", "name", 10, True)]
    assert r["folder"] == {"cid": 106, "path": SHOW, "videos": 5}
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
    res = c.post(delete, json={"series_id": sid, "whole": True}, headers=h).json()
    assert res == {"deleted": 1, "folder_removed": True, "note": ""} and fake.deleted[-1] == "106"
    assert not (media / "劇集" / "中国新说唱 (2017)").exists()
    assert not app.state.db.one("SELECT 1 FROM items WHERE id=?", (sid,))
    assert c.post(delete, json={"series_id": sid, "whole": True}, headers=h).status_code == 400  # 已經沒有這部劇

    # 115 真的 id：19 位，超過 JavaScript 能精確表示的範圍
    big, big_dir = 2856394156427519845, 2856394156427519000
    fake.dirs[big_dir] = ("春晚 (2026)", 102)
    for i, n in enumerate(["开场.mp4", "零点.mp4"]):
        fake.files.append({"fid": big + i, "cid": big_dir, "n": n, "pc": f"sw{i}".ljust(17, "x"), "s": 900_000_000, "te": T0 + 200 + i})
    assert not app.state.strm_sync.run(FULL).errors
    app.state.scanner.scan_all()
    gala = app.state.db.one("SELECT id FROM items WHERE type='Series' AND name='春晚'")["id"]
    r = c.get(files, params={"series": gala, "season": 1}, headers=h).json()
    assert [f["file_id"] for f in r["files"]] == [str(big), str(big + 1)] and r["folder"]["cid"] == str(big_dir)
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
