"""整理 115 網盤：問 MoviePilot 每個資料夾整理後叫什麼、找出對不上的、整個資料夾交給它預覽和執行、清掉搬空的舊資料夾
（假 115 + 假 MoviePilot）。"""

import json
import posixpath
import re
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
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
    執行時在假 115 上搬，目標已經有同名檔案就跳過。"""

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
        if path != "/api/v1/transfer/manual":
            return httpx.Response(404)
        items = [body["fileitem"]] if "fileitem" in body else body["fileitems"]
        files = []
        for it in items:
            files += self.expand(int(it["fileid"])) if it["type"] == "dir" else [(int(it["fileid"]), it["path"])]
        out = []
        for fid, src in files:
            tid = str(body.get("media_id") or self.media_of(src) or "")
            if not tid:
                out.append({"source": src, "success": False, "message": "未识别到媒体信息", "state": "failed"})
                continue
            title, year, kind = self.MEDIA[tid]
            if body.get("type_name") == "电影":
                kind = "movie"
            season = body.get("season") or self.season_of(src)
            stem, ext = posixpath.splitext(posixpath.basename(src))
            if kind == "tv" and body.get("episode_format"):  # 照集數定位取集號
                ep = template_episode(body["episode_format"], posixpath.basename(src))
                stem = str(ep) if ep else "沒有"
            name = self.file_name(tid, season, stem, ext) if kind == "tv" else f"{title} ({year}){ext}"
            if not name:
                out.append({"source": src, "success": False, "message": "未识别到文件集数", "state": "failed"})
                continue
            # 沒給整理到哪：照它的目錄設定放進媒體庫（這裡是同步目錄外的 /媒體庫）
            dest = body.get("target_path") or ("/媒體庫/電影" if kind == "movie" else "/媒體庫/劇集")
            folder = f"{dest}/{self.folder_name(tid)}"
            target = f"{folder}/Season {season}/{name}" if kind == "tv" else f"{folder}/{name}"
            base = posixpath.basename(src)
            target = src if base in self.same else f"/别处/{base}" if base in self.outside else target
            item = {"source": src, "target": target, "success": True, "title": title,
                    "type": "电视剧" if kind == "tv" else "电影", "season": season if kind == "tv" else None,
                    "episode": int(re.search(r"E(\d+)", name).group(1)) if kind == "tv" else None}
            if not body["preview"]:
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
    assert job["found"] == 7 and r["counts"] == {"series": 4, "movie": 3, "episodes": 3}

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
    assert parts["d119"]["recognized"] == [{"title": "虚天战纪", "type": "电影", "season": None, "count": 2}]
    assert parts["loose"]["recognized"] == [{"title": "凡人修仙传", "type": "电视剧", "season": 2, "count": 3}]
    assert any("MoviePilot 認成第 2 季，媒體庫裡是第 1 季" in n for n in pv["notes"])
    assert sorted(pv["folders"]) == ["凡人修仙传 (2020) {tmdbid=106449}", "虚天战纪 (2025) {tmdbid=282348}"]
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
    assert out["summary"]["skipped"] == 1 and any("會搬出同步目錄" in n for n in out["notes"])
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
    # 虚天战纪 上、下認成同一部電影：第二支跳過；康熙 EP02 目標已經有，跳過
    assert (job.total, job.done, job.failed, job.title) == (7, 5, 2, "整理 2 個資料夾") and not job.errors
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
