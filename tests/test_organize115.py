"""整理 115 網盤：問 MoviePilot 每個資料夾整理後叫什麼、找出對不上的、整個資料夾交給它預覽和執行、清掉搬空的舊資料夾
（假 115 + 假 MoviePilot）。"""

import json
import posixpath
import re
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.organize115 import OrganizeError
from embyserver.p115 import P115Service
from embyserver.reorganize import template_episode
from embyserver.strm_sync import FULL

from test_incremental import T0, Fake115
from test_reorganize import wait

FANREN = "F 凡人修仙传{tmdbid-106449} 更176｜停更｜预计第二季度更新"
XUTIAN = "虚天战纪.导演剪辑版 (2025) [tmdb-282348]"
EXECUTE = "/web/api/115/organize/execute"


class OrganizeMP:
    """MoviePilot：自己辨識名稱（資料夾、上一層、上上層的 tmdbid 標記或片名），照「片名 (年份) {tmdbid=…}/Season N/
    片名 - SxxEyy - 第 N 集」命名。和真的一樣，名稱裡的「第二季度」會被認成第 2 季。整理資料夾時把裡面的影片都算進來，
    執行時在假 115 上搬，目標已經有同名檔案就跳過。
    目錄設定（dirs）：預設只有一項「下載目錄 /下載 → 媒體庫 /媒體庫」（在同步目錄外，加類型資料夾），和真的一樣，
    沒指定整理到哪時它照這一項放；target-path 問得到它（match=False 時問不到）。history 裡的檔案有整理紀錄：
    真的整理時沒帶 reorganize 就當成「已整理過」跳過（預覽不看紀錄）。queue：真的整理時都放進它的整理佇列在背景做
    （accepted），這時候還沒搬。"""

    MEDIA = {"106449": ("凡人修仙传", 2020, "tv"), "6836": ("康熙来了", 2004, "tv"), "292388": ("斗破苍穹", 2017, "tv"),
             "9": ("流浪", 2019, "tv"), "157336": ("星际穿越", 2014, "movie"), "14160": ("飞屋环游记", 2009, "movie"),
             "282348": ("虚天战纪", 2025, "movie")}
    KEYWORDS = {"康熙来了": "6836", "Interstellar": "157336", "Up.2009": "14160"}

    def __init__(self, fake115: Fake115):
        self.f = fake115
        self.calls = []
        self.same, self.outside = set(), set()
        self.delete_source = False
        self.next_cid = 300
        self.version = "v3.0.10-1"
        self.recommend = "{ep}"  # 推薦的集數定位；空字串是推薦不出來
        self.dirs = [{"name": "115 媒體庫", "storage": "u115", "download_path": "/下載", "monitor_type": "monitor",
                      "library_storage": "u115", "library_path": "/媒體庫", "library_type_folder": True,
                      "library_category_folder": False, "overwrite_mode": "never", "renaming": True}]
        self.match = True  # /transfer/manual/target-path 挑得出目錄
        self.history = set()  # 有成功整理紀錄的檔案（115 路徑）
        self.queue = False
        self.plugin = False  # 裝了 Mi302 整理助手外掛
        self.names = True  # 外掛會算名字（1.1.0）；False 是舊版 1.0.0，"broken" 是算名字出錯
        self.renamed = []  # 外掛改過的：(type, 舊名, 新名)

    # ---- 辨識 ----
    def media_of(self, path):
        parts = path.rstrip("/").split("/")[-3:]
        for part in reversed(parts):
            m = re.search(r"tmdb(?:id)?[=\-](\d+)", part)
            if m:
                return m.group(1)
        for part in reversed(parts):
            for word, tid in self.KEYWORDS.items():
                if word in part:
                    return tid
        return None

    def season_of(self, path):
        for part in reversed(path.rstrip("/").split("/")[-3:-1]):
            m = re.fullmatch(r"Season (\d+)", part)
            if m:
                return int(m.group(1))
            m = re.search(r"第([一二三])季", part)
            if m:
                return "一二三".index(m.group(1)) + 1
        return 1

    def folder_name(self, tid):
        title, year, _ = self.MEDIA[tid]
        return f"{title} ({year}) {{tmdbid={tid}}}"

    def file_name(self, tid, season, stem, ext):
        title, year, kind = self.MEDIA[tid]
        if kind == "movie":
            return f"{title} ({year}){ext}"
        m = re.search(r"(\d+)(?!.*\d)", stem)
        if not m:
            return None
        ep = int(m.group(1))
        return f"{title} - S{season:02d}E{ep:02d} - 第 {ep} 集{ext}"

    def named(self, src, body):
        """認片、算名字（相對於媒體庫目錄），整理預覽和外掛算名字共用。body：tmdbid／media_id、類型、季、集數定位。"""
        tid = str(body.get("media_id") or body.get("tmdbid") or self.media_of(src) or "")
        if not tid:
            return {"source": src, "success": False, "target": None, "message": "未识别到媒体信息"}
        title, year, kind = self.MEDIA[tid]
        if "电影" in (body.get("type_name"), body.get("type")):
            kind = "movie"
        season = body.get("season") or self.season_of(src)
        base = posixpath.basename(src)
        stem, ext = posixpath.splitext(base)
        if kind == "tv" and body.get("episode_format"):  # 照集數定位取集號
            ep = template_episode(body["episode_format"], base)
            stem = str(ep) if ep else "沒有"
        name = self.file_name(tid, season, stem, ext) if kind == "tv" else f"{title} ({year}){ext}"
        if not name:
            return {"source": src, "success": False, "target": None, "message": "未识别到文件集数"}
        folder = self.folder_name(tid)
        # 和真的一樣：片名帶年份；集號照檔名解析，認成電影也一樣（檔名有 SxxEyy 就有集號）
        ep = re.search(r"E(\d+)", name) if kind == "tv" else re.search(r"(?i)S\d+E(\d+)", base)
        return {"source": src, "success": True, "message": "", "title": f"{title} ({year})",
                "target": f"{folder}/Season {season}/{name}" if kind == "tv" else f"{folder}/{name}",
                "type": "电视剧" if kind == "tv" else "电影", "season": season if kind == "tv" else None,
                "episode": int(ep.group(1)) if ep else None}

    # ---- Mi302 整理助手外掛：照清單的順序在假 115 上改名；1.1.0 起也照上面的規則算名字 ----
    def plugin_api(self, sub, body):
        if not self.plugin:
            return httpx.Response(404, json={"detail": "Not Found"})
        if sub == "status":
            return httpx.Response(200, json={"enabled": True, "version": "1.1.0" if self.names else "1.0.0", "busy": False,
                                             "features": ["rename", "names"] if self.names else ["rename"]})
        if sub == "names":
            if self.names == "broken":
                return httpx.Response(200, json={"success": False, "message": "AttributeError: 改版了"})
            items = [dict(self.named(it["path"], body), path=it["path"]) for it in body["items"]
                     if posixpath.splitext(it["path"])[1] in (".mp4", ".mkv", ".ass")]
            return httpx.Response(200, json={"success": True, "items": items})
        if sub == "rename":
            results = []
            for it in body["items"]:
                fid = int(it["fileid"])
                if it["type"] == "dir":
                    self.f.dirs[fid] = (it["name"], self.f.dirs[fid][1])
                else:
                    self.f.file(fid)["n"] = it["name"]
                self.renamed.append((it["type"], it["old"], it["name"]))
                results.append({"fileid": it["fileid"], "old": it["old"], "name": it["name"], "type": it["type"],
                                "ok": True, "message": ""})
            self.plugin_job = {"success": True, "state": "done", "total": len(results), "done": len(results),
                               "results": results}
            return httpx.Response(200, json={"success": True, "job": "j1"})
        if sub == "job":
            return httpx.Response(200, json=self.plugin_job)
        return httpx.Response(200, json={"success": True})

    # ---- 假 115 ----
    def path_of(self, cid):
        return "/" + "/".join(a["name"] for a in self.f.ancestors(cid)[1:])

    def dir_by_path(self, path):
        return next((c for c in self.f.dirs if c and self.path_of(c) == path), None)

    def expand(self, cid):
        out = [(f["fid"], self.path_of(cid) + "/" + f["n"]) for f in self.f.files if f["cid"] == cid]
        for c, (_, parent) in list(self.f.dirs.items()):
            if parent == cid:
                out += self.expand(c)
        return out

    def mkdirs(self, path):
        cid = 0
        parts = path.strip("/").split("/")
        for i, name in enumerate(parts):
            found = self.dir_by_path("/" + "/".join(parts[:i + 1]))
            if found is None:
                self.next_cid += 1
                found = self.next_cid
                self.f.dirs[found] = (name, cid)
                self.f.event(17, found, is_dir=True)
            cid = found
        return cid

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/login/access-token":
            return httpx.Response(200, json={"access_token": "jwt"})
        if request.headers.get("Authorization") != "Bearer jwt":
            return httpx.Response(403, json={"detail": "需要登入"})
        body = json.loads(request.content or b"{}")
        self.calls.append((path, body or dict(request.url.params)))
        if path == "/api/v1/system/env":
            return httpx.Response(200, json={"success": True, "data": {
                "VERSION": self.version, "TV_RENAME_FORMAT": "a/Season {{season}}/b", "MOVIE_RENAME_FORMAT": "a/b"}})
        if path == "/api/v1/transfer/episode-format/recommend":
            return httpx.Response(200, json={"success": True, "data": {"episode_format": self.recommend, "rule_name": "純數字"}}
                                  if self.recommend else {"success": False, "message": "样本不足"})
        if path == "/api/v1/storage/directories":
            return httpx.Response(200, json={"success": True, "data": self.dirs})
        if path == "/api/v1/transfer/manual/target-path":
            return httpx.Response(200, json={"success": True, "data": {"target_storage": "u115", "target_path": "/媒體庫"}
                                             if self.match else {"target_storage": None, "target_path": None}})
        if path == "/api/v1/transfer/manual/history":
            items = [body["fileitem"]] if "fileitem" in body else body["fileitems"]
            paths = [p for it in items for _, p in (self.expand(int(it["fileid"])) if it["type"] == "dir" else [(0, it["path"])])]
            n = sum(1 for p in paths if p in self.history)
            return httpx.Response(200, json={"success": True, "data": {"reorganize": bool(n), "history_count": n}})
        if path == "/api/v1/transfer/name":
            p, kind = request.url.params["path"], request.url.params["filetype"]
            tid = self.media_of(p)
            if not tid:
                return httpx.Response(200, json={"success": False, "message": "未识别到媒体信息"})
            if kind == "dir":
                return httpx.Response(200, json={"success": True, "data": {"name": self.folder_name(tid)}})
            stem, ext = posixpath.splitext(posixpath.basename(p))
            name = self.file_name(tid, self.season_of(p), stem, ext)
            return httpx.Response(200, json={"success": True, "data": {"name": name}} if name else
                                  {"success": False, "message": "未识别到文件集数"})
        if path.startswith("/api/v1/plugin/Mi302Organizer/"):
            return self.plugin_api(path.rsplit("/", 1)[-1], body)
        if path != "/api/v1/transfer/manual":
            return httpx.Response(404)
        items = [body["fileitem"]] if "fileitem" in body else body["fileitems"]
        files = []
        for it in items:
            files += self.expand(int(it["fileid"])) if it["type"] == "dir" else [(int(it["fileid"]), it["path"])]
        out = []
        for fid, src in files:
            item = self.named(src, body)
            if not item["success"]:
                out.append(dict(item, state="failed"))
                continue
            # 沒給整理到哪：照它的目錄設定放進媒體庫（這裡是同步目錄外的 /媒體庫，加類型資料夾）
            dest = body.get("target_path") or "/媒體庫"
            if body.get("library_type_folder", not body.get("target_path")):
                dest += "/電影" if item["type"] == "电影" else "/劇集"
            base = posixpath.basename(src)
            target = src if base in self.same else f"/别处/{base}" if base in self.outside else f"{dest}/{item['target']}"
            item["target"] = target
            if not body["preview"] and src in self.history and not body.get("reorganize"):
                item.update(success=False, state="skipped", message=f"{base} 已整理过")
            elif not body["preview"] and self.queue:
                item["state"] = "accepted"
            elif not body["preview"]:
                tdir = self.dir_by_path(posixpath.dirname(target))
                if tdir is not None and any(f["cid"] == tdir and f["n"] == posixpath.basename(target) for f in self.f.files):
                    item.update(success=False, state="skipped", message="目标文件已存在")
                else:
                    cid = self.mkdirs(posixpath.dirname(target))
                    self.f.file(fid).update(cid=cid, n=posixpath.basename(target))
                    self.f.event(6, fid)
                    item["state"] = "completed"
            out.append(item)
        if not body["preview"] and self.delete_source:
            for it in items:
                if it["type"] == "dir":
                    self.f.dirs.pop(int(it["fileid"]), None)
        return httpx.Response(200, json={"success": True, "data": {"items": out}})


def setup(tmp_path: Path, login: bool = True):
    fake = Fake115()
    fake.dirs.update({
        110: (FANREN, 102), 119: (XUTIAN, 110),
        111: ("康熙来了 (2004)", 102), 112: ("康熙来了 (2004) {tmdbid=6836}", 102), 113: ("Season 1", 112),
        114: ("D 斗破苍穹{tmdbid-292388} 更186", 102), 115: ("第一季", 114), 116: ("Season 2", 114),
        117: ("流浪 (2019) {tmdbid=9}", 102), 118: ("Season 1", 117),
        120: ("星际穿越 Interstellar 2014 4K", 101),
    })
    files = [(40, 110, "1.mp4"), (41, 110, "2.mp4"), (42, 110, "3.mp4"), (43, 119, "虚天战纪 上.mp4"), (44, 119, "虚天战纪 下.mp4"),
             (50, 111, "康熙来了 EP01.mp4"), (51, 111, "康熙来了 EP02.mp4"), (52, 113, "康熙来了 - S01E02 - 第 2 集.mp4"),
             (60, 115, "01.mp4"), (61, 116, "斗破苍穹 - S02E01 - 第 1 集.mp4"), (62, 114, "特别篇.mp4"),
             (70, 118, "流浪 - S01E01 - 第 1 集.mp4"), (80, 120, "Interstellar.2014.mkv"), (81, 101, "Up.2009.1080p.mkv")]
    for i, (fid, cid, name) in enumerate(files):
        fake.files.append({"fid": fid, "cid": cid, "n": name, "pc": f"pc{fid}".ljust(17, "x"), "s": 900_000_000, "te": T0 + i})
    fake.event(2, 2)
    media = tmp_path / "media"
    mp_cfg = {"url": "http://mp.test", "scrape_after_sync": False, "fill_after_full_sync": False}
    mp_cfg.update({"username": "admin", "password": "pw"} if login else {"api_token": "t"})
    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")},
        "users": [{"name": "admin", "password": "pw", "admin": True}],
        "libraries": [{"name": "劇集", "type": "tvshows", "paths": [str(media / "劇集")]},
                      {"name": "電影", "type": "movies", "paths": [str(media / "電影")]}],
        "moviepilot": mp_cfg,
        "p115": {"cookies": "UID=1", "strm": {"tasks": [{"remote": "/影視", "local": str(media)}], "request_delay": 0,
                                               "scan_after_sync": False, "delete_stale": True, "download_metadata": False}},
    }), scan_on_start=False)
    svc = P115Service(app.state.db, initial_cookies="UID=1", transport=httpx.MockTransport(fake.handler))
    for holder in (app.state, app.state.strm_sync, app.state.dupes, app.state.prober, app.state.redirector):
        holder.p115 = svc
    svc.download_url = lambda pc, ua="": f"https://cdn.115.test/{pc}"
    svc.export_poll = 0
    mp = OrganizeMP(fake)
    app.state.moviepilot._transport = httpx.MockTransport(mp.handler)
    app.state.reorganizer.sync_delay = 0
    assert not app.state.strm_sync.run(FULL).errors
    app.state.scanner.scan_all()
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    return app, fake, mp, media, c, h


def check(app, c, h, refresh=False):
    assert c.post("/web/api/115/organize/check", json={"refresh": refresh}, headers=h).json()["started"]
    wait(lambda: not app.state.organizer.job.running)
    assert not app.state.organizer.job.error


def listing(c, h, **params):
    return c.get("/web/api/115/organize", params=params, headers=h).json()


def name_calls(mp):
    return [b for p_, b in mp.calls if p_ == "/api/v1/transfer/name"]


def preview(c, h, unit, parts=None, **extra):
    """預設整理到同一層（假 MoviePilot 的目錄設定在同步目錄外的 /媒體庫）。"""
    body = {"id": unit["id"], "parts": parts or {}, "scrape": False, "target": "parent", **extra}
    return c.post("/web/api/115/organize/preview", json=body, headers=h)


def sent_bodies(mp):
    return [b for p_, b in mp.calls if p_ == "/api/v1/transfer/manual"]


def test_moviepilot_decides_what_is_nonstandard(tmp_path: Path):
    app, fake, mp, media, c, h = setup(tmp_path)
    assert c.get("/web/api/115/organize").status_code == 401
    before = listing(c, h)
    # 還沒問過 MoviePilot：只列媒體庫裡集號不對的（不用問它）
    assert before["unchecked"] == before["folders"] == 9
    assert {u["name"] for u in before["items"]} == {FANREN, "康熙来了 (2004)", "D 斗破苍穹{tmdbid-292388} 更186"}
    assert all(u["reasons"][0].startswith("媒體庫裡") and not u["checked"] for u in before["items"])
    dp = next(u for u in before["items"] if u["name"].startswith("D 斗破"))
    assert (dp["ep_guessed"], dp["ep_unknown"]) == (1, 1) and dp["reasons"] == ["媒體庫裡 1 集的集號是從檔名猜的、1 集認不出集號"]
    check(app, c, h)
    r = listing(c, h)
    job = r["job"]
    assert (job["total"], job["todo"], job["done"], r["unchecked"]) == (9, 9, 9, 0)
    units = {u["name"]: u for u in r["items"]}
    # 名稱和 MoviePilot 給的一樣、集號也對的（康熙来了 {tmdbid=6836}、流浪）不列
    assert set(units) == {FANREN, "康熙来了 (2004)", "D 斗破苍穹{tmdbid-292388} 更186", "Dark", "星际穿越 Interstellar 2014 4K",
                          "Old Movie (2001)", "Up.2009.1080p"}
    assert job["found"] == 7 and r["counts"] == {"series": 4, "movie": 3, "episodes": 3, "held": 0}

    fr = units[FANREN]
    assert fr["mp_name"] == "凡人修仙传 (2020) {tmdbid=106449}" and fr["error"] == "" and fr["checked"]
    assert fr["reasons"][1:] == ["資料夾名稱和 MoviePilot 的不一樣", "3 支影片直接放在資料夾裡（MoviePilot 會放進季資料夾）",
                                 "檔名和 MoviePilot 的不一樣"]
    assert fr["mp_files"][0] == ["1.strm", "凡人修仙传 - S02E01 - 第 1 集.strm"]  # MoviePilot 把「预计第二季度」認成第 2 季
    # 子資料夾（別部影片）和直接放著的影片分開送，都讓 MoviePilot 自己認
    assert [(p["key"], p["label"], p["videos"]) for p in fr["parts"]] == [("d119", XUTIAN, 2), ("loose", "直接放在資料夾裡的影片", 3)]

    kx = units["康熙来了 (2004)"]
    assert kx["merge_into"] == {"name": "康熙来了 (2004) {tmdbid=6836}", "path": "/影視/劇集/康熙来了 (2004) {tmdbid=6836}"}
    assert units["Dark"]["error"] == "未识别到媒体信息" and units["Dark"]["reasons"] == ["MoviePilot 認不出來：未识别到媒体信息"]
    assert units["Up.2009.1080p"]["kind"] == "movie_file" and "沒有自己的資料夾" in units["Up.2009.1080p"]["reasons"][0]
    assert units["星际穿越 Interstellar 2014 4K"]["mp_name"] == "星际穿越 (2014) {tmdbid=157336}"
    assert [u["name"] for u in listing(c, h, kind="movie")["items"]] == ["Old Movie (2001)", "Up.2009.1080p", "星际穿越 Interstellar 2014 4K"]
    assert [u["name"] for u in listing(c, h, kind="episodes")["items"]] == ["D 斗破苍穹{tmdbid-292388} 更186", FANREN, "康熙来了 (2004)"]
    assert [u["name"] for u in listing(c, h, q="凡人")["items"]] == [FANREN]

    # 再檢查一次：問過、沒變的不再問；資料夾裡多了影片的重問；refresh 全部重問
    mp.calls.clear()
    check(app, c, h)
    assert name_calls(mp) == [] and listing(c, h)["job"]["todo"] == 0
    fake.files.append({"fid": 45, "cid": 110, "n": "4.mp4", "pc": "pc45".ljust(17, "x"), "s": 900_000_000, "te": T0 + 50})
    assert not app.state.strm_sync.run(FULL).errors
    app.state.scanner.scan_all()
    check(app, c, h)
    asked = {b["path"] for b in name_calls(mp)}
    assert f"/影視/劇集/{FANREN}" in asked and all(FANREN in p for p in asked)
    mp.calls.clear()
    check(app, c, h, refresh=True)
    assert len({b["path"] for b in name_calls(mp) if b["filetype"] == "dir"}) == 7  # 沒有自己資料夾的電影只問檔名


def test_preview_lets_moviepilot_recognize(tmp_path: Path):
    app, fake, mp, media, c, h = setup(tmp_path)
    check(app, c, h)
    units = {u["id"]: u for u in listing(c, h)["items"]}
    mp.calls.clear()
    pv = preview(c, h, units["d110"]).json()
    sent = [b for p_, b in mp.calls if p_ == "/api/v1/transfer/manual"]
    # 什麼都沒指定：不送 TMDB 編號、類型、季，整理到同一層
    assert len(sent) == 2 and all("media_id" not in b and "season" not in b and "type_name" not in b for b in sent)
    assert sent[0]["fileitem"] == {"storage": "u115", "type": "dir", "path": f"/影視/劇集/{FANREN}/{XUTIAN}/", "name": XUTIAN,
                                   "basename": XUTIAN, "fileid": "119", "parent_fileid": "110"}
    assert [fi["name"] for fi in sent[1]["fileitems"]] == ["1.mp4", "2.mp4", "3.mp4"] and sent[1]["target_path"] == "/影視/劇集"
    parts = {p["key"]: p for p in pv["parts"]}
    assert parts["d119"]["recognized"] == [{"title": "虚天战纪 (2025)", "type": "电影", "season": None, "count": 2}]
    assert parts["loose"]["recognized"] == [{"title": "凡人修仙传 (2020)", "type": "电视剧", "season": 2, "count": 3}]
    assert any("MoviePilot 認成第 2 季，媒體庫裡是第 1 季" in n for n in pv["notes"])
    assert pv["folders"] == ["/影視/劇集/凡人修仙传 (2020) {tmdbid=106449}/Season 2", "/影視/劇集/虚天战纪 (2025) {tmdbid=282348}"]
    assert all("只會留一個" in i["warnings"][0] for i in pv["items"] if i["part"] == XUTIAN)  # 上、下兩支認成同一部電影
    assert pv["token"]

    # 認錯的季：只在那一部分指定
    mp.calls.clear()
    fixed = preview(c, h, units["d110"], {"loose": {"season": 1}}).json()
    sent = [b for p_, b in mp.calls if p_ == "/api/v1/transfer/manual"]
    assert "season" not in sent[0] and sent[1]["season"] == 1
    assert {p["key"]: p["recognized"][0]["season"] for p in fixed["parts"]}["loose"] == 1
    assert not any("季" in n for n in fixed["notes"])
    assert fixed["items"][-1]["target"] == "/影視/劇集/凡人修仙传 (2020) {tmdbid=106449}/Season 1/凡人修仙传 - S01E03 - 第 3 集.mp4"

    # 已經照格式命名（新位置和原本一樣）的不送：資料夾改成只送其他影片，直接放著的影片少送那一支
    mp.same.update({"2.mp4", "虚天战纪 上.mp4"})
    mp.calls.clear()
    narrowed = preview(c, h, units["d110"], {"loose": {"season": 1}}).json()
    assert [(p["key"], p["ok"]) for p in narrowed["parts"]] == [("d119", 1), ("loose", 2)] and narrowed["token"]
    assert narrowed["summary"]["skipped"] == 2 and any("1 個已經照格式命名" in n for n in narrowed["notes"])
    mp.same.clear()
    mp.calls.clear()
    assert c.post(EXECUTE, json={"tokens": [narrowed["token"]]}, headers=h).status_code == 200
    wait(lambda: not app.state.reorganizer.job.running)
    sent = sent_bodies(mp)
    assert [[fi["name"] for fi in b["fileitems"]] for b in sent] == [["虚天战纪 下.mp4"], ["1.mp4", "3.mp4"]]
    assert sent[0]["fileitems"][0]["fileid"] == "44" and sent[0]["fileitems"][0]["parent_fileid"] == "119"

    # 會把同步目錄裡的檔案搬出去的也不送
    units = {u["id"]: u for u in listing(c, h)["items"]}
    mp.outside.add("康熙来了 EP01.mp4")
    out = preview(c, h, units["d111"]).json()
    # EP02 的目標（康熙来了 (2004) {tmdbid=6836}/Season 1）已經有同一集：MoviePilot 預覽不查，Mi302 查到就不送
    assert out["summary"]["skipped"] == 2 and any("1 個會搬出同步目錄、1 個目標已經有同名檔案" in n for n in out["notes"])
    ep02 = next(i for i in out["items"] if i["name"] == "康熙来了 EP02.mp4")
    assert ep02["skip"] == "exists" and "重複" in ep02["message"]
    mp.outside.clear()

    # 集數定位：指定了就送給 MoviePilot
    mp.calls.clear()
    preview(c, h, units["d111"], {"all": {"format": "康熙来了 EP{ep}"}})
    assert sent_bodies(mp)[0]["episode_format"] == "康熙来了 EP{ep}"
    assert preview(c, h, units["d111"], {"all": {"format": "沒有集號"}}).status_code == 400

    # 舊版 MoviePilot 不認預覽，會直接整理：版本太舊或查不到都不送
    for version in ("v2.11.1", "", "dev"):
        mp.version, mp.calls = version, []
        app.state.moviepilot._preview_ok_at = 0
        r = preview(c, h, units["d111"])
        assert r.status_code == 400 and "太舊" in r.text and not sent_bodies(mp)
    mp.version = "v3.0.10-1"

    # 指定的參數檢查；電影不送季
    assert preview(c, h, units["d111"], {"all": {"tmdbid": "abc"}}).status_code == 400
    mp.calls.clear()
    preview(c, h, units["f81"], {"file": {"type": "movie", "tmdbid": "14160", "season": 3}})
    sent = [b for p_, b in mp.calls if p_ == "/api/v1/transfer/manual"][0]
    assert (sent["fileitem"]["name"], sent["media_id"], sent["type_name"]) == ("Up.2009.1080p.mkv", "14160", "电影")
    assert "season" not in sent
    assert preview(c, h, {"id": "d999"}).status_code == 400


def test_execute_moves_and_cleans_up(tmp_path: Path):
    app, fake, mp, media, c, h = setup(tmp_path)
    check(app, c, h)
    units = {u["id"]: u for u in listing(c, h)["items"]}
    fr = preview(c, h, units["d110"], {"loose": {"season": 1}}).json()
    kx = preview(c, h, units["d111"]).json()
    cleanup = [{"cid": 110, "path": units["d110"]["path"]}, {"cid": 111, "path": units["d111"]["path"]}]
    assert c.post(EXECUTE, json={"tokens": [fr["token"]], "cleanup": [{"cid": 114, "path": "/x"}]}, headers=h).status_code == 400
    mp.calls.clear()
    assert c.post(EXECUTE, json={"tokens": [fr["token"], kx["token"]], "cleanup": cleanup}, headers=h).status_code == 200
    wait(lambda: not app.state.reorganizer.job.running)
    sent = [b for p_, b in mp.calls if p_ == "/api/v1/transfer/manual"]
    assert [b.get("season") for b in sent] == [None, 1, None]  # 執行時照預覽時指定的
    job = app.state.reorganizer.job
    # 虚天战纪 上、下認成同一部電影：第二支跳過；康熙 EP02 目標已經有同一集，預覽就不送
    assert (job.total, job.done, job.failed, job.title) == (6, 5, 1, "整理 2 個資料夾") and not job.errors
    folders = {i["name"]: i for i in job.items if i["state"] in ("kept", "removed")}
    assert folders[units["d110"]["path"]]["state"] == "kept" and "1 支影片" in folders[units["d110"]["path"]]["message"]
    assert folders[units["d111"]["path"]]["state"] == "kept"
    assert {f["n"] for f in fake.files if f["cid"] == 113} == {"康熙来了 - S01E01 - 第 1 集.mp4", "康熙来了 - S01E02 - 第 2 集.mp4"}

    # 增量同步把 strm 搬到新位置；重新掃描、再問一次 MoviePilot 後，整理好的不再列出
    wait(lambda: not app.state.strm_sync.result.running and app.state.strm_sync.result.mode == "incremental")
    assert (media / "劇集/凡人修仙传 (2020) {tmdbid=106449}/Season 1/凡人修仙传 - S01E03 - 第 3 集.strm").exists()
    app.state.scanner.scan_all()
    check(app, c, h)
    names = {u["name"] for u in listing(c, h)["items"]}
    assert "凡人修仙传 (2020) {tmdbid=106449}" not in names and "康熙来了 (2004)" in names


def test_cleanup_skips_folder_moviepilot_already_removed(tmp_path: Path):
    app, fake, mp, media, c, h = setup(tmp_path)
    check(app, c, h)
    units = {u["id"]: u for u in listing(c, h)["items"]}
    fake.files = [f for f in fake.files if f["fid"] != 52]  # 目標沒有 EP02：整個資料夾一起送
    pv = preview(c, h, units["d111"]).json()
    mp.delete_source = True
    c.post(EXECUTE, json={"tokens": [pv["token"]], "cleanup": [{"cid": 111, "path": units["d111"]["path"]}]}, headers=h)
    wait(lambda: not app.state.reorganizer.job.running)
    kept = [i for i in app.state.reorganizer.job.items if i["state"] == "kept"]
    assert kept and "已經不在原本的位置" in kept[0]["message"] and "111" not in fake.deleted


def test_needs_moviepilot_login(tmp_path: Path):
    app, fake, mp, media, c, h = setup(tmp_path, login=False)
    r = c.post("/web/api/115/organize/check", json={}, headers=h)
    assert r.status_code == 400 and "帳號密碼" in r.text
    r = listing(c, h)
    assert not r["ready"]["login"] and all(not u["checked"] and u["ep_guessed"] + u["ep_unknown"] for u in r["items"])
    assert preview(c, h, r["items"][0]).status_code == 400  # 手動整理也要帳號登入


def test_recommend_episode_format(tmp_path: Path):
    """集數定位：先請 MoviePilot 推薦，推薦不出來才用 Mi302 從檔名看的，標明是誰給的。"""
    app, fake, mp, media, c, h = setup(tmp_path)
    units = {u["id"]: u for u in listing(c, h)["items"]}
    rec = c.post("/web/api/115/organize/recommend", json={"id": "d110", "part": "loose"}, headers=h).json()
    assert (rec["format"], rec["source"]) == ("{ep}", "moviepilot")
    sent = [b for p_, b in mp.calls if p_ == "/api/v1/transfer/episode-format/recommend"][-1]
    assert [fi["name"] for fi in sent["fileitems"]] == ["1.mp4", "2.mp4", "3.mp4"]
    mp.recommend = ""
    rec = c.post("/web/api/115/organize/recommend", json={"id": "d110", "part": "loose"}, headers=h).json()
    assert (rec["format"], rec["source"]) == ("{ep}.{a}", "mi302") and "MoviePilot 推薦不出來" in rec["note"]
    assert c.post("/web/api/115/organize/recommend", json={"id": "d110", "part": "nope"}, headers=h).status_code == 400
    assert units["d111"]


def test_folder_picked_in_browse(tmp_path: Path):
    """瀏覽 115 裡挑的資料夾：釘在清單最上面；不在同步目錄裡的預設照 MoviePilot 的目錄設定整理，新位置在同步目錄外只提醒。"""
    app, fake, mp, media, c, h = setup(tmp_path)
    fake.dirs[200] = ("待整理", 0)
    for i, n in enumerate(["Up.2009.mkv", "Interstellar.2014.mkv"]):
        fake.files.append({"fid": 90 + i, "cid": 200, "n": n, "pc": f"dz{i}".ljust(17, "x"), "s": 900_000_000, "te": T0})
    folder = "/web/api/115/organize/folder"
    assert c.post(folder, json={"cid": 0, "path": "/"}, headers=h).status_code == 400
    unit = c.post(folder, json={"cid": 200, "path": "/待整理"}, headers=h).json()
    assert (unit["id"], unit["kind"], unit["in_sync"], unit["pinned"], unit["videos"]) == ("d200", "folder", False, True, 2)
    assert unit["error"] == "未识别到媒体信息"  # MoviePilot 看資料夾名稱「待整理」認不出來；檔案各自認得
    r = listing(c, h)
    assert [u["id"] for u in r["pinned"]] == ["d200"] and "d200" not in [u["id"] for u in r["items"]]
    mp.calls.clear()
    pv = c.post("/web/api/115/organize/preview", json={"id": unit["id"], "scrape": False}, headers=h).json()  # 沒指定：照目錄設定
    sent = sent_bodies(mp)[0]
    assert pv["target"] == "auto" and "target_path" not in sent and sent["fileitem"]["fileid"] == "200"
    assert pv["summary"]["ok"] == 2 and all("Mi302 不會替它產生 strm" in i["warnings"][0] for i in pv["items"])
    # 指定整理到的 115 資料夾
    mp.calls.clear()
    pv = preview(c, h, unit, target="path", target_path="/影視/電影").json()
    assert sent_bodies(mp)[0]["target_path"] == "/影視/電影" and not any(i["warnings"] for i in pv["items"])
    assert preview(c, h, unit, target="path", target_path="").status_code == 400
    assert c.post(EXECUTE, json={"tokens": [pv["token"]], "cleanup": [{"cid": "200", "path": "/待整理"}]}, headers=h).status_code == 200
    wait(lambda: not app.state.reorganizer.job.running)
    assert app.state.reorganizer.job.done == 2 and fake.deleted[-1] == "200"  # 搬空了，移到回收站
    assert c.delete("/web/api/115/organize/folder/d200", headers=h).json() == {"ok": True}
    assert listing(c, h)["pinned"] == []


def test_delete_a_movie_from_the_list(tmp_path: Path):
    app, fake, mp, media, c, h = setup(tmp_path)
    check(app, c, h)
    strm = media / "電影" / "Up.2009.1080p.strm"
    assert strm.exists()
    assert c.post("/web/api/115/organize/delete", json={"id": "f81"}, headers=h).json() == \
        {"deleted": 1, "folder_removed": False, "note": ""}
    assert "81" in fake.deleted and not strm.exists()
    assert "Up.2009.1080p" not in {u["name"] for u in listing(c, h)["items"]}
    assert c.post("/web/api/115/organize/delete", json={"id": "d120"}, headers=h).json()["folder_removed"]  # 電影資料夾
    assert "120" in fake.deleted and not (media / "電影" / "星际穿越 Interstellar 2014 4K").exists()


LIBRARY = {"name": "影視庫", "storage": "local", "download_path": "/downloads", "monitor_type": "monitor",
           "library_storage": "u115", "library_path": "/影視", "library_type_folder": True,
           "library_category_folder": False, "overwrite_mode": "never"}


def test_auto_target_uses_the_library_the_folder_is_in(tmp_path: Path):
    """照 MoviePilot 的目錄設定：它自己挑目錄只看下載目錄，媒體庫裡的資料夾對不上。它整理對話框的「按類型分類」
    「按類別分類」「刮削元數據」「複用歷史識別信息」都關掉，所以已經在媒體庫目錄裡的留在現在的分類資料夾，只改名稱。"""
    app, fake, mp, media, c, h = setup(tmp_path)
    mp.dirs.append(dict(LIBRARY))
    check(app, c, h)
    units = {u["id"]: u for u in listing(c, h)["items"]}
    mp.calls.clear()
    pv = preview(c, h, units["d111"], target="auto").json()
    sent = sent_bodies(mp)[0]
    assert (sent["target_storage"], sent["target_path"]) == ("u115", "/影視/劇集")
    assert [sent[k] for k in ("library_type_folder", "library_category_folder", "scrape", "from_history")] == [False] * 4
    assert pv["notes"][0] == "已經在 MoviePilot 的媒體庫目錄「影視庫」裡：留在 /影視/劇集，只改資料夾和檔名（不加類型、類別資料夾）"
    assert pv["folders"] == ["/影視/劇集/康熙来了 (2004) {tmdbid=6836}/Season 1"]  # 留在原本的分類資料夾

    # 那一項只收電影：劇集不用它，讓 MoviePilot 自己挑（問得到 /媒體庫）
    mp.dirs[-1]["media_type"] = "电影"
    mp.calls.clear()
    pv = preview(c, h, units["d111"], target="auto").json()
    assert "target_path" not in sent_bodies(mp)[0] and pv["notes"][0] == "MoviePilot 照它的目錄設定整理到媒體庫 /媒體庫（不加類型、類別資料夾）"

    # 都對不上：不送預覽，說清楚（它預覽時只會說「整理任务处理失败」）
    mp.dirs, mp.match = [], False
    mp.calls.clear()
    r = preview(c, h, units["d111"], target="auto")
    assert r.status_code == 400 and "目錄設定" in r.text and "同一層" in r.text and not sent_bodies(mp)


def test_moviepilot_history_is_reorganized(tmp_path: Path):
    """MoviePilot 整理過的檔案（紀錄還在）：沒帶 reorganize 會被它當成「已整理過」跳過，所以和它的網頁一樣帶上。"""
    app, fake, mp, media, c, h = setup(tmp_path)
    check(app, c, h)
    units = {u["id"]: u for u in listing(c, h)["items"]}
    mp.history = {"/影視/劇集/康熙来了 (2004)/康熙来了 EP01.mp4"}
    pv = preview(c, h, units["d111"]).json()
    assert any("MoviePilot 有 1 條成功整理的紀錄" in n for n in pv["notes"])
    mp.calls.clear()
    assert c.post(EXECUTE, json={"tokens": [pv["token"]]}, headers=h).status_code == 200
    wait(lambda: not app.state.reorganizer.job.running)
    assert sent_bodies(mp)[0]["reorganize"] is True
    states = {i["name"]: (i["state"], i["message"]) for i in app.state.reorganizer.job.items}
    assert states["康熙来了 EP01.mp4"][0] == "completed"

    # 沒有紀錄的不帶
    app2, fake2, mp2, _, c2, h2 = setup(tmp_path / "b")
    check(app2, c2, h2)
    unit = {u["id"]: u for u in listing(c2, h2)["items"]}["d111"]
    pv = preview(c2, h2, unit).json()
    mp2.calls.clear()
    c2.post(EXECUTE, json={"tokens": [pv["token"]]}, headers=h2)
    wait(lambda: not app2.state.reorganizer.job.running)
    assert "reorganize" not in sent_bodies(mp2)[0]


def test_names_that_differ_only_in_word_order_are_the_same():
    from embyserver.organize115 import _same_name

    # MoviePilot 解析時把效果倒過來排：它自己取的名稱再問一次會換順序
    assert _same_name("浪浪山小妖怪.Nobody.2025.WEB-DL DV HQ.2160p.H265.DTS 5.1", "浪浪山小妖怪.Nobody.2025.WEB-DL HQ DV.2160p.H265.DTS 5.1")
    assert _same_name("Title.2023.4K", "title.2023.4k")
    assert not _same_name("画江湖之天罡.2023.4K(1)", "画江湖之天罡.A Portrait of Jianghu： The Legend.2023.4k")
    assert not _same_name("康熙来了 (2004)", "康熙来了 (2004) {tmdbid=6836}")


def test_crowded_movie_folder_is_described():
    from embyserver.organize115 import Unit, judge

    def unit(parent, loose, others):
        return Unit("f1", "movie_file", f"/cms/電影/{parent}/画江湖之天罡.2023.4K(1)", 1, 0, "画江湖之天罡.2023.4K(1)", 1,
                    "画江湖之天罡", 2023, 1, loose, [], [], checked=True, others=others)

    u = unit("H-画江湖之天罡-2023-[tmdb=1221210]", 1, 1)  # 另一支在子資料夾裡
    judge(u, 3, 2)
    assert u.reasons == ["資料夾裡還有另外 1 支影片（MoviePilot 會給每部電影自己的資料夾；同一部的重複檔案可以先到「整理 → 重複檔案」清掉）"]
    u = unit("动画电影", 5, 4)  # 分類資料夾
    judge(u, 3, 2)
    assert u.reasons == ["沒有自己的資料夾（MoviePilot 會放進自己的資料夾）"]


def test_latest_overwrite_mode_blocks_in_place_renames():
    """覆蓋模式「保留最新」：目標不存在時 MoviePilot 會先刪掉目標資料夾裡同一集的其他版本（只避開目標本身），
    在同一個資料夾裡改名時來源也在那裡，會被刪掉，所以不送。"""
    from embyserver.organize115 import Part, _overwrite, _view

    part = Part("all", "整個資料夾", 1, "/影視/劇集/X", None, 1)
    item = {"source": "/影視/劇集/X/Season 1/x.01.mp4", "target": "/影視/劇集/X/Season 1/X - S01E01.mp4", "success": True}
    moved = {**item, "target": "/影視/劇集/X (2020)/Season 1/X - S01E01.mp4"}
    v = _view(item, part, ["/影視"], "latest")
    assert (v["ok"], v["skip"]) == (False, "latest") and "保留最新" in v["message"]
    assert _view(moved, part, ["/影視"], "latest")["ok"] and _view(item, part, ["/影視"], "never")["ok"]
    dirs = [{"monitor_type": "monitor", "library_storage": "u115", "library_path": "/影視/", "overwrite_mode": "size"},
            {"monitor_type": "monitor", "library_storage": "u115", "library_path": "/影視", "overwrite_mode": "latest"},
            {"monitor_type": "", "library_storage": "u115", "library_path": "/別的", "overwrite_mode": "latest"}]
    assert _overwrite(dirs, "/影視") == "latest" and _overwrite(dirs, "/別的") == "never" and _overwrite([], "/影視") == "never"


def test_vague_moviepilot_failure_gets_a_hint():
    from embyserver.organize115 import Part, _view

    part = Part("all", "整個資料夾", 1, "/影視/電影/X", None, 1)
    v = _view({"source": "/影視/電影/X/x.mkv", "success": False, "message": "整理任务处理失败，请稍后重试"}, part, ["/影視"])
    assert not v["ok"] and "目錄設定對不上" in v["message"]


def test_existing_target_is_not_sent_unless_moviepilot_overwrites(tmp_path: Path):
    """目標已經有同名檔案：覆蓋模式「不覆蓋」時 MoviePilot 會失敗（之前失敗過的還會在背景照舊計畫重試），不送；
    會覆蓋的只提醒。MoviePilot 放進背景佇列的（retry_wait、accepted）算「背景處理」，不算整理好也不算失敗。"""
    app, fake, mp, media, c, h = setup(tmp_path)
    check(app, c, h)
    unit = {u["id"]: u for u in listing(c, h)["items"]}["d111"]
    pv = preview(c, h, unit).json()
    assert [i["name"] for i in pv["items"] if i["ok"]] == ["康熙来了 EP01.mp4"] and pv["summary"]["skipped"] == 1
    dup = next(i for i in pv["items"] if i["skip"] == "exists")
    # 網頁上「刪掉這支」要的：這支的 id、所在資料夾、兩邊的大小
    assert (dup["file_id"], dup["parent_cid"], dup["size"], dup["exists_size"]) == ("51", "111", 900_000_000, 900_000_000)
    assert "刪掉這支" in dup["message"]

    # 那個媒體庫目錄會覆蓋：送，但提醒
    mp.dirs.append({**LIBRARY, "library_path": "/影視/劇集", "overwrite_mode": "always"})
    pv = preview(c, h, unit).json()
    ep02 = next(i for i in pv["items"] if i["name"] == "康熙来了 EP02.mp4")
    assert ep02["ok"] and "會用這支蓋掉它" in ep02["warnings"][0]

    # 刪掉這支：只刪這一個檔案（移到 115 回收站），目標那一份不動
    r = c.post("/web/api/115/delete", json={"parent": dup["parent_cid"], "ids": [dup["file_id"]]}, headers=h)
    assert r.status_code == 200 and r.json()["names"] == ["康熙来了 EP02.mp4"] and fake.deleted[-1] == "51"
    assert any(f["fid"] == 52 for f in fake.files)

    # 執行時 MoviePilot 放進背景重試：照實說
    from embyserver.reorganize import ReorgJob
    job = ReorgJob()
    app.state.moviepilot.transfer = lambda *a, **k: [
        {"source": "/a/1.mp4", "state": "retry_wait", "message": "已提交重新整理，后台将自动处理"},
        {"source": "/a/2.mp4", "state": "completed", "success": True}]
    app.state.reorganizer._run_items({"scrape": False, "target_path": None}, {"fileitems": [], "label": "x", "count": 2,
                                                                             "single": False}, job)
    assert (job.done, job.queued, job.failed) == (1, 1, 0) and "照上次的計畫在背景重試" in job.items[0]["message"]


def test_same_level_goes_above_a_folder_named_after_the_movie(tmp_path: Path):
    """「H-画江湖之天罡-2023-[tmdb=1221210]」裡有一支「(1)」和一個之前整理錯、套在裡面的電影資料夾：
    「同一層」要到 H- 資料夾的上一層（分類資料夾），不能把新的電影資料夾建在 H- 裡；整理完 H- 空了也一起清。"""
    app, fake, mp, media, c, h = setup(tmp_path)
    mp.MEDIA = {**mp.MEDIA, "1221210": ("画江湖之天罡", 2023, "movie")}
    fake.dirs.update({130: ("H-画江湖之天罡-2023-[tmdb=1221210]", 101), 131: ("画江湖之天罡 (2023) {tmdbid=1221210}", 130)})
    fake.files += [{"fid": 90, "cid": 130, "n": "画江湖之天罡.2023.4K(1).mkv", "pc": "hj1".ljust(17, "x"), "s": 900_000_000, "te": T0 + 50},
                   {"fid": 91, "cid": 131, "n": "画江湖之天罡 (2023).mkv", "pc": "hj2".ljust(17, "x"), "s": 900_000_000, "te": T0 + 51}]
    assert not app.state.strm_sync.run(FULL).errors
    app.state.scanner.scan_all()
    check(app, c, h)
    units = {u["id"]: u for u in listing(c, h)["items"]}
    loose, nested = units["f90"], units["d131"]
    holder = "/影視/電影/H-画江湖之天罡-2023-[tmdb=1221210]"
    assert loose["reasons"][0].startswith("資料夾裡還有另外 1 支影片")
    assert "套在另一個以片名命名的資料夾「H-画江湖之天罡-2023-[tmdb=1221210]」裡" in nested["reasons"][0]
    assert loose["cleanup"] == [{"cid": "130", "path": holder}]
    assert nested["cleanup"] == [{"cid": "131", "path": f"{holder}/画江湖之天罡 (2023) {{tmdbid=1221210}}"}, {"cid": "130", "path": holder}]

    mp.calls.clear()
    pv = preview(c, h, loose).json()
    assert sent_bodies(mp)[0]["target_path"] == "/影視/電影"  # 不是 H- 資料夾
    assert pv["folders"] == ["/影視/電影/画江湖之天罡 (2023) {tmdbid=1221210}"]

    # 套在裡面的那一份：搬到分類資料夾底下，搬空的 131 移到回收站；H- 裡還有「(1)」，留著
    mp.calls.clear()
    pv = preview(c, h, nested).json()
    assert sent_bodies(mp)[0]["target_path"] == "/影視/電影"
    assert c.post(EXECUTE, json={"tokens": [pv["token"]], "cleanup": nested["cleanup"]}, headers=h).status_code == 200
    wait(lambda: not app.state.reorganizer.job.running)
    folders = {i["name"]: i["state"] for i in app.state.reorganizer.job.items if i["state"] in ("kept", "removed")}
    assert folders == {f"{holder}/画江湖之天罡 (2023) {{tmdbid=1221210}}": "removed", holder: "kept"}


def run_all(app, c, h, **body):
    r = c.post("/web/api/115/organize/all", json={"target": "parent", "cleanup": True, **body}, headers=h)
    assert r.status_code == 200, r.text
    wait(lambda: not app.state.organizer.batch.running)
    return c.get("/web/api/115/organize/all", headers=h).json()


def test_organize_all_skips_the_ones_that_need_a_look(tmp_path: Path):
    """全部整理：清單上的一個一個預覽，沒問題的直接整理，有問題的跳過並寫原因；最後同步一次。"""
    app, fake, mp, media, c, h = setup(tmp_path)
    check(app, c, h)
    mp.calls.clear()
    b = run_all(app, c, h)
    assert (b["total"], b["done"], b["organized"], b["files"], b["skipped"], b["failed"], b["error"], b["synced"]) == \
        (7, 7, 3, 3, 4, 0, "", "started")
    why = {x["id"]: x["why"] for x in b["results"]}
    assert set(why) == {"d114", "d103", "d110", "f1"}
    assert "不會整理（未识别到文件集数）" in why["d114"]  # 特别篇.mp4 認不出集號：整部劇跳過，不整理一半
    assert "只會留一個" in why["d110"] and "認成第 2 季，媒體庫裡是第 1 季" in why["d110"]
    ran = {posixpath.basename((b_.get("fileitem") or b_["fileitems"][0])["path"].rstrip("/"))
           for b_ in sent_bodies(mp) if not b_["preview"]}
    assert ran == {"康熙来了 EP01.mp4", "Up.2009.1080p.mkv", "星际穿越 Interstellar 2014 4K"}
    # 進度（不含明細）也在清單和工作狀態裡
    assert listing(c, h)["batch"]["result_count"] == 4
    assert c.get("/web/api/115/organize/job", headers=h).json()["batch"]["results"] == []


def test_organize_all_can_be_stopped_and_holds_the_lock(tmp_path: Path):
    import threading

    app, fake, mp, media, c, h = setup(tmp_path)
    check(app, c, h)
    unit = {u["id"]: u for u in listing(c, h)["items"]}["d111"]
    token = preview(c, h, unit).json()["token"]
    org, gate, started = app.state.organizer, threading.Event(), threading.Event()
    real = org._organize_one

    def slow(*a):
        started.set()
        gate.wait(5)
        return real(*a)

    org._organize_one = slow
    assert c.post("/web/api/115/organize/all", json={"target": "parent"}, headers=h).status_code == 200
    assert started.wait(5)
    assert c.post("/web/api/115/organize/all", json={}, headers=h).status_code == 400  # 已經在全部整理
    r = c.post(EXECUTE, json={"tokens": [token]}, headers=h)
    assert r.status_code == 400 and "已經有一批" in r.text  # 同一把鎖：這時候不能單獨整理
    assert c.post("/web/api/115/organize/all/stop", headers=h).json()["batch"]["stopping"]
    gate.set()
    wait(lambda: not org.batch.running)
    assert org.batch.stopped and org.batch.done == 1
    assert c.post(EXECUTE, json={"tokens": [token]}, headers=h).status_code == 200  # 停下來後鎖放開了


def test_organize_all_gives_up_when_previews_keep_failing(tmp_path: Path):
    from embyserver.organize115 import OrganizeError

    app, fake, mp, media, c, h = setup(tmp_path)
    check(app, c, h)

    def down(*a, **k):
        raise OrganizeError("MoviePilot 預覽失敗：連不上 mp.test")

    app.state.organizer._preview = down
    b = run_all(app, c, h)
    assert b["done"] == 5 and b["skipped"] == 5 and "連續 5 個預覽出錯" in b["error"] and "連不上" in b["error"]


def test_queued_files_still_get_a_sync(tmp_path: Path):
    """MoviePilot 只收進它背景佇列的（accepted）：一個都沒整理好也要同步。它做完之後本機的 strm 只靠增量同步搬，
    它通知媒體伺服器只會讓 Mi302 重新掃描本機。"""
    app, fake, mp, media, c, h = setup(tmp_path)
    check(app, c, h)
    mp.queue = True
    token = preview(c, h, {u["id"]: u for u in listing(c, h)["items"]}["d111"]).json()["token"]
    assert c.post(EXECUTE, json={"tokens": [token]}, headers=h).status_code == 200
    wait(lambda: not app.state.reorganizer.job.running)
    job = app.state.reorganizer.job
    assert (job.done, job.failed, job.synced) == (0, 0, "started") and job.queued
    wait(lambda: not app.state.strm_sync.result.running)
    b = run_all(app, c, h)
    assert (b["files"], b["failed"], b["synced"]) == (0, 0, "started") and b["queued"] and b["organized"]


def test_organizing_needs_115_login(tmp_path: Path):
    """沒登入 115：預覽、執行、全部整理先擋下來，不拿鎖、不開執行緒（不然每個資料夾都列不出來，連續出錯才停）；
    問 MoviePilot 檢查只要 MoviePilot。"""
    app, fake, mp, media, c, h = setup(tmp_path)
    check(app, c, h)
    unit = {u["id"]: u for u in listing(c, h)["items"]}["d111"]
    token = preview(c, h, unit).json()["token"]
    app.state.strm_sync.p115.set_cookies("")
    assert listing(c, h)["ready"]["p115"] is False
    mp.calls.clear()
    for r in (preview(c, h, unit), c.post(EXECUTE, json={"tokens": [token]}, headers=h),
              c.post("/web/api/115/organize/all", json={"target": "parent"}, headers=h)):
        assert r.status_code == 400 and "要先登入 115" in r.text
    with pytest.raises(OrganizeError, match="要先登入 115"):
        app.state.organizer.organize_all("", "", "parent", "", True)
    assert not sent_bodies(mp) and not app.state.organizer.batch.started and not app.state.reorganizer.job.started
    assert app.state.reorganizer.hold()  # 鎖沒被拿走
    app.state.reorganizer.release()
    check(app, c, h, refresh=True)


def test_shutdown_wakes_waiting_work_before_closing_the_database(tmp_path: Path):
    """程式結束時：整理完在等 115 記下變動的馬上醒來、不再開同步；所有背景工作停了才關資料庫。"""
    app, fake, mp, media, c, h = setup(tmp_path)
    check(app, c, h)
    reorg = app.state.reorganizer
    reorg.sync_delay = 600
    token = preview(c, h, {u["id"]: u for u in listing(c, h)["items"]}["d111"]).json()["token"]
    assert c.post(EXECUTE, json={"tokens": [token]}, headers=h).status_code == 200
    wait(lambda: reorg.job.current.startswith("等 115"))
    started = time.monotonic()
    with TestClient(app):
        pass  # 跑一次 lifespan 的結束
    assert time.monotonic() - started < 5
    assert not reorg.job.running and reorg.job.done and reorg.job.synced == ""
    st = app.state
    for s in (st.organizer, reorg, st.dupes, st.strm_sync, st.moviepilot, st.person_names, st.prober, st.scanner,
              st.backup, st.updater):
        assert s.workers.join(0) == []
    with pytest.raises(sqlite3.ProgrammingError):
        st.db.query("SELECT 1")


def flaky_mp(app, mp, fail):
    """MoviePilot 前面插一層：fail(request) 回傳例外就丟出去（模擬它被系統停掉、途中斷線）。"""
    def handler(request):
        exc = fail(request)
        if exc:
            raise exc
        return mp.handler(request)

    app.state.moviepilot._transport = httpx.MockTransport(handler)


def test_organize_all_waits_for_moviepilot_and_leaves_held_ones(tmp_path: Path, monkeypatch):
    """MoviePilot 連不上（被系統停掉、重新啟動中）：等它回來再做同一個，不算跳過。標了「先不整理」的、
    一次要送太多支影片的這次不做。"""
    import embyserver.organize115 as og

    monkeypatch.setattr(og, "MP_DOWN_POLL", 0)
    monkeypatch.setattr(og, "MP_COOLDOWN", 0)
    app, fake, mp, media, c, h = setup(tmp_path)
    check(app, c, h)
    held = c.post("/web/api/115/organize/hold", json={"id": "d120", "hold": True}, headers=h).json()
    assert held["held"]
    lst = listing(c, h)
    assert lst["counts"]["held"] == 1 and lst["items"][-1]["id"] == "d120"  # 排到最後
    assert [u["id"] for u in listing(c, h, kind="held")["items"]] == ["d120"]
    refused = {"n": 3}  # 版本檢查、兩次等它的時候都連不上，之後恢復

    def fail(request):
        if refused["n"] > 0:
            refused["n"] -= 1
            return httpx.ConnectError("[Errno 111] Connection refused", request=request)

    flaky_mp(app, mp, fail)
    mp.calls.clear()
    b = run_all(app, c, h, max_videos=2)  # 凡人修仙传那一部分有 3 支：這次不做
    assert (b["total"], b["done"], b["organized"], b["skipped"], b["failed"], b["held"], b["mp_down"], b["error"]) == \
        (5, 5, 2, 3, 0, 2, 1, "")
    sent = [b_ for b_ in sent_bodies(mp)]
    assert not any("星际穿越" in json.dumps(b_, ensure_ascii=False) for b_ in sent)
    assert not any(FANREN in json.dumps(b_, ensure_ascii=False) for b_ in sent)


def test_organize_all_does_not_resend_after_moviepilot_drops(tmp_path: Path, monkeypatch):
    """真的整理到一半 MoviePilot 斷線：它可能還在背景做，這一個記成失敗、不重送；等它有回應再做下一個。"""
    import embyserver.organize115 as og

    monkeypatch.setattr(og, "MP_DOWN_POLL", 0)
    monkeypatch.setattr(og, "MP_COOLDOWN", 0)
    app, fake, mp, media, c, h = setup(tmp_path)
    check(app, c, h)
    runs = []

    def fail(request):
        if request.url.path == "/api/v1/transfer/manual" and not json.loads(request.content)["preview"]:
            runs.append(request)
            if len(runs) == 1:
                return httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)

    flaky_mp(app, mp, fail)
    b = run_all(app, c, h)
    assert (b["organized"], b["failed"], b["mp_down"], b["error"]) == (2, 1, 1, "")
    assert len(runs) == 3  # 斷線的那一個沒有再送
    failed = [x for x in b["results"] if x["kind"] == "failed"]
    assert "斷線" in failed[0]["why"] and "不送" in failed[0]["why"]


def test_rename_only_folders_go_to_the_plugin(tmp_path: Path):
    """結構已經對、只是名字不對的資料夾：交給 Mi302 整理助手直接改名（先檔案、字幕，最後資料夾），不送 MoviePilot 整理。
    要併進旁邊已經有的資料夾、沒裝外掛的，照舊走整理。"""
    app, fake, mp, media, c, h = setup(tmp_path)
    mp.MEDIA = {**mp.MEDIA, "777": ("测试剧", 2020, "tv")}
    fake.dirs.update({130: ("C-测试剧-2020-[tmdb=777]", 102), 131: ("Season 1", 130),
                      140: ("H-流浪-2019-[tmdb=9]", 102), 141: ("Season 1", 140)})
    for fid, cid, n in [(90, 131, "测试剧.E01.mp4"), (91, 131, "测试剧.E01.ass"), (92, 131, "测试剧.E02.mp4"),
                        (93, 141, "流浪.E02.mp4")]:
        fake.files.append({"fid": fid, "cid": cid, "n": n, "pc": f"rn{fid}".ljust(17, "x"), "s": 900_000_000, "te": T0})
    folder = "/web/api/115/organize/folder"
    unit = c.post(folder, json={"cid": 130, "path": "/影視/劇集/C-测试剧-2020-[tmdb=777]"}, headers=h).json()
    assert not any(n.startswith("只需要改名") for n in preview(c, h, unit).json()["notes"])  # 沒裝外掛：照舊走整理
    mp.plugin = True
    app.state.moviepilot._plugin_checked = (0.0, False)  # 剛裝好：不等 5 分鐘的快取
    pv = preview(c, h, unit).json()
    assert pv["notes"][0].startswith("只需要改名") and pv["summary"]["ok"] == 3
    mp.calls.clear()
    assert c.post(EXECUTE, json={"tokens": [pv["token"]]}, headers=h).status_code == 200
    wait(lambda: not app.state.reorganizer.job.running)
    job = app.state.reorganizer.job
    assert (job.done, job.failed, job.errors) == (3, 0, [])
    assert not [b for b in sent_bodies(mp) if not b["preview"]]  # 沒有送 MoviePilot 整理
    assert mp.renamed == [("file", "测试剧.E01.ass", "测试剧 - S01E01 - 第 1 集.ass"),
                          ("file", "测试剧.E01.mp4", "测试剧 - S01E01 - 第 1 集.mp4"),
                          ("file", "测试剧.E02.mp4", "测试剧 - S01E02 - 第 2 集.mp4"),
                          ("dir", "C-测试剧-2020-[tmdb=777]", "测试剧 (2020) {tmdbid=777}")]  # Season 1 本來就對
    # 旁邊已經有「流浪 (2019) {tmdbid=9}」：要併進去，原地改名會撞名，照舊走整理
    unit = c.post(folder, json={"cid": 140, "path": "/影視/劇集/H-流浪-2019-[tmdb=9]"}, headers=h).json()
    pv = preview(c, h, unit).json()
    assert pv["token"] and not any(n.startswith("只需要改名") for n in pv["notes"])


def test_wrong_tmdbid_in_folder_name_needs_a_look(tmp_path: Path):
    """資料夾名裡的 TMDB 編號錯了，MoviePilot 照編號認成別部片（有集號的檔案認成電影，或片名、年份都對不上）：
    要人看一下，全部整理時跳過，不會把整部劇搬進別部片的資料夾。片名或年份有一樣對得上的照常整理。"""
    app, fake, mp, media, c, h = setup(tmp_path)
    base = dict(mp.MEDIA)

    def review(media_9):
        mp.MEDIA = {**base, "9": media_9}  # 「流浪 (2019) {tmdbid=9}」的 9 查到的是這部
        check(app, c, h, refresh=True)
        unit = next(u for u in listing(c, h)["items"] if u["id"] == "d117")
        return preview(c, h, unit).json()["review"]

    assert review(("Wandering", 2019, "tv")) == []  # 英文片名，年份一樣
    assert review(("流浪", 2023, "tv")) == []  # 片名一樣，年份不同
    r = review(("斗罗大陆Ⅱ绝世唐门", 2023, "tv"))
    assert "認成「斗罗大陆Ⅱ绝世唐门 (2023)」，片名和年份（2019）都和資料夾對不上：資料夾名裡的 TMDB 編號 9 多半不對" in r[0]
    r = review(("The Ragamuffin", 1916, "movie"))
    assert "有 1 個有集號的檔案被 MoviePilot 認成電影「The Ragamuffin (1916)」：資料夾名裡的 TMDB 編號 9 多半不對" in r[0]
    mp.calls.clear()
    why = {x["id"]: x["why"] for x in run_all(app, c, h)["results"]}
    assert "認成電影「The Ragamuffin (1916)」" in why["d117"].split("；")[0]
    assert not [b for b in sent_bodies(mp) if not b["preview"] and "流浪" in json.dumps(b, ensure_ascii=False)]
    # 指定了 TMDB 編號：照指定的整理，不再提醒
    unit = next(u for u in listing(c, h)["items"] if u["id"] == "d117")
    parts = {p["key"]: {"tmdbid": "9"} for p in unit["parts"]}
    assert not any("TMDB 編號" in x for x in preview(c, h, unit, parts=parts).json()["review"])


def test_plugin_names_match_moviepilot_preview(tmp_path: Path):
    """外掛會算名字（1.1.0）時，預覽不跑 MoviePilot 的整理預覽，改請外掛照它的規則算：結果要和它的預覽一模一樣
    （改名、整理都照這份結果做，不一樣就會改錯）。舊版外掛、外掛算不出來時改用它的預覽。"""
    app, fake, mp, media, c, h = setup(tmp_path)
    check(app, c, h)
    mp.plugin = True
    units = {u["id"]: u for u in listing(c, h)["items"]}

    def run(unit, names):
        mp.names = names
        app.state.moviepilot._plugin_checked = (0.0, False)
        mp.calls.clear()
        pv = preview(c, h, unit).json()
        return pv, [b for p_, b in mp.calls if p_ == "/api/v1/transfer/manual"]

    def key(pv):
        return sorted((i["source"], i["target"], i["ok"], i["skip"], i["title"], i["episode"]) for i in pv["items"])

    # 凡人修仙传：子資料夾（整個送）加上直接放著的影片；斗破苍穹：有一支認不出集號
    for uid in ("d110", "d114"):
        old, sent_old = run(units[uid], False)
        new, sent_new = run(units[uid], True)
        assert sent_old and not sent_new
        assert key(new) == key(old) and new["review"] == old["review"] and bool(new["token"]) == bool(old["token"])
        assert any(n.startswith("新名字由 MoviePilot 的「Mi302 整理助手」") for n in new["notes"])
        broken, sent = run(units[uid], "broken")
        assert sent and key(broken) == key(old) and any("算名字失敗" in n for n in broken["notes"])
