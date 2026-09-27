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
