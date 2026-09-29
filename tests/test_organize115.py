"""整理 115 網盤：命名格式變正則、找出不規範的資料夾、整個資料夾交給 MoviePilot 預覽和執行、清掉搬空的舊資料夾
（假 115 + 假 MoviePilot）。"""

import json
import posixpath
import re
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.organize115 import DEFAULT_MOVIE, DEFAULT_TV, Formats, _layout, folder_key, folder_tag
from embyserver.p115 import P115Service
from embyserver.strm_sync import FULL

from test_incremental import T0, Fake115
from test_reorganize import wait

TV = ("{{title}}{% if year %} ({{year}}){% endif %} {tmdbid={{tmdbid}}}/Season {{season}}/{{title}} - {{season_episode}}"
      "{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif %}{{fileExt}}")
MOVIE = "{{title}}{% if year %} ({{year}}){% endif %} {tmdbid={{tmdbid}}}/{{title}}{% if year %} ({{year}}){% endif %}{{fileExt}}"
FANREN = "F 凡人修仙传{tmdbid-106449} 更176｜停更｜预计第二季度更新"
EXECUTE = "/web/api/moviepilot/reorganize/execute"


def test_template_to_patterns():
    tv = _layout(TV, True)
    assert tv.match("folder", "康熙来了 (2004) {tmdbid=6836}") and tv.match("folder", "康熙来了 {tmdbid=6836}")
    assert not tv.match("folder", "康熙来了 (2004)") and not tv.match("folder", FANREN)
    assert tv.match("season", "Season 1") and tv.match("season", "Season 01") and not tv.match("season", "第一季")
    assert tv.match("file", "康熙来了 - S01E03") and tv.match("file", "康熙来了 - S01E03 - 第 3 集")
    assert not tv.match("file", "176") and not tv.match("file", "康熙来了 第3集")
    assert tv.render_folder({"title": "康熙来了", "year": 2004, "tmdbid": "6836"}) == "康熙来了 (2004) {tmdbid=6836}"
    assert tv.render_folder({"title": "康熙来了", "year": "", "tmdbid": "6836"}) == "康熙来了 {tmdbid=6836}"
    assert tv.uses("folder", "tmdbid") and not _layout(DEFAULT_TV, True).uses("folder", "tmdbid")
    movie = _layout(MOVIE, False)
    assert movie.match("folder", "飞屋环游记 (2009) {tmdbid=14160}") and movie.season is None
    # 看不懂的模板：改用預設格式，說明原因
    f = Formats.build("{% for x in y %}{{x}}{% endfor %}/{{title}}", "{% if a %}/b{% endif %}{{title}}", "moviepilot")
    assert f.tv_template == DEFAULT_TV and f.movie_template == DEFAULT_MOVIE and "看不懂" in f.note
    # 沒有季資料夾的格式
    flat = _layout("{{title}} ({{year}})/{{title}} - {{season_episode}}{{fileExt}}", True)
    assert flat.season is None and flat.match("folder", "X (2020)")


def test_folder_tag_and_key():
    assert folder_tag(FANREN) == "106449" and folder_tag("Dark [tmdb=70523]") == "70523"
    assert folder_tag("康熙来了 (2004)") == "" and folder_tag("2004 {tmdbid=}") == ""
    assert {folder_key(n) for n in ["康熙来了 (2004) {tmdbid=6836}", "康熙来了 (2004)", "康熙来了（2004）", "康熙来了  (2004) "]} \
        == {"康熙来了 (2004)"}
    assert folder_key("康熙来了 (2004)") != folder_key("康熙来了 (2015)")


class OrganizeMP:
    """MoviePilot：命名格式是上面的 TV、MOVIE；整理資料夾時把裡面（含子資料夾）的影片、字幕都算進來，
    執行時在假 115 上搬，目標已經有同名檔案就跳過（整理到指定資料夾時 MoviePilot 的覆蓋模式是 never）。"""

    MEDIA = {"106449": ("凡人修仙传", 2020), "6836": ("康熙来了", 2004), "292388": ("斗破苍穹", 2017),
             "157336": ("星际穿越", 2014), "14160": ("飞屋环游记", 2009)}

    def __init__(self, fake115: Fake115):
        self.f = fake115
        self.calls = []
        self.same, self.outside = set(), set()  # 這些檔名預覽時新位置和原本一樣／在同步目錄外
        self.delete_source = False  # 搬空後 MoviePilot 自己把來源資料夾刪掉
        self.next_cid = 300

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
        for i, name in enumerate(path.strip("/").split("/")):
            found = self.dir_by_path("/" + "/".join(path.strip("/").split("/")[:i + 1]))
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
        self.calls.append((path, body))
        if path == "/api/v1/system/env":
            return httpx.Response(200, json={"success": True, "data": {"VERSION": "v3.0.10-1", "TV_RENAME_FORMAT": TV,
                                                                        "MOVIE_RENAME_FORMAT": MOVIE}})
        if path != "/api/v1/transfer/manual":
            return httpx.Response(404)
        items = [body["fileitem"]] if "fileitem" in body else body["fileitems"]
        files = []
        for it in items:
            files += self.expand(int(it["fileid"])) if it["type"] == "dir" else [(int(it["fileid"]), it["path"])]
        title, year = self.MEDIA[str(body["media_id"])]
        folder = f"{title} ({year}) {{tmdbid={body['media_id']}}}"
        out = []
        for fid, src in files:
            name = posixpath.basename(src)
            stem, ext = posixpath.splitext(name)
            ep = None
            if body["type_name"] == "电影":
                target = f"{body['target_path']}/{folder}/{title} ({year}){ext}"
            else:
                m = re.search(r"(\d+)(?!.*\d)", stem)
                if not m:
                    out.append({"source": src, "success": False, "message": "未识别到文件集数", "state": "failed"})
                    continue
                ep, season = int(m.group(1)), body["season"]
                target = f"{body['target_path']}/{folder}/Season {season}/{title} - S{season:02d}E{ep:02d} - 第 {ep} 集{ext}"
            target = src if name in self.same else f"/别处/{name}" if name in self.outside else target
            item = {"source": src, "target": target, "success": True, "episode": ep, "season": body.get("season")}
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
        110: (FANREN, 102),
        111: ("康熙来了 (2004)", 102), 112: ("康熙来了 (2004) {tmdbid=6836}", 102), 113: ("Season 1", 112),
        114: ("D 斗破苍穹{tmdbid-292388} 更186", 102), 115: ("第一季", 114), 116: ("Season 2", 114),
        117: ("流浪 (2019) {tmdbid=9}", 102), 118: ("Season 1", 117),
        120: ("星际穿越 Interstellar 2014 4K", 101),
    })
    files = [(40, 110, "1.mp4"), (41, 110, "2.mp4"), (42, 110, "3.mp4"),
             (50, 111, "康熙来了 EP01.mp4"), (51, 111, "康熙来了 EP02.mp4"), (52, 113, "康熙来了 - S01E02 - 第 2 集.mp4"),
             (60, 115, "01.mp4"), (61, 116, "斗破苍穹 - S02E01 - 第 1 集.mp4"), (62, 114, "特别篇.mp4"),
             (70, 118, "流浪 - S01E01.mp4"), (80, 120, "Interstellar.2014.mkv"), (81, 101, "Up.2009.1080p.mkv")]
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


def listing(c, h, **params):
    return c.get("/web/api/115/organize", params=params, headers=h).json()


def preview(c, h, unit, **body):
    body = {"id": unit["id"], "tmdbid": unit["tmdbid"], "type": unit["type"],
            "seasons": {p["key"]: p["season"] for p in unit["parts"]}, "scrape": False, **body}
    return c.post("/web/api/115/organize/preview", json=body, headers=h)


def test_find_nonstandard_folders(tmp_path: Path):
    app, fake, mp, media, c, h = setup(tmp_path)
    assert c.get("/web/api/115/organize").status_code == 401
    r = listing(c, h)
    assert r["formats"]["source"] == "moviepilot" and r["formats"]["tv"] == TV and r["ready"]["login"]
    units = {u["name"]: u for u in r["items"]}
    # 照格式命名的（康熙来了 {tmdbid=6836}、流浪）不列；Dark 沒有年份、tmdbid 和季資料夾，列出來
    assert set(units) == {FANREN, "康熙来了 (2004)", "D 斗破苍穹{tmdbid-292388} 更186", "Dark", "星际穿越 Interstellar 2014 4K",
                          "Old Movie (2001)", "Up.2009.1080p"}
    assert r["counts"] == {"series": 4, "movie": 3} and r["total"] == 7

    fr = units[FANREN]
    assert (fr["id"], fr["kind"], fr["type"], fr["videos"], fr["tmdbid"], fr["tmdb_from"]) == ("d110", "series", "tv", 3, "106449", "name")
    assert fr["reasons"][0] == "資料夾名稱不照命名格式（tmdbid 的寫法不對）"
    assert any("直接放在劇集資料夾裡" in x for x in fr["reasons"]) and any("3 個檔名不照命名格式" in x for x in fr["reasons"])
    assert fr["parts"] == [{"key": "all", "label": "整個資料夾", "videos": 3, "season": 1}] and fr["parent"] == "/影視/劇集"

    kx = units["康熙来了 (2004)"]
    assert kx["merge_into"]["name"] == "康熙来了 (2004) {tmdbid=6836}" and (kx["tmdbid"], kx["tmdb_from"]) == ("6836", "merge")

    dp = units["D 斗破苍穹{tmdbid-292388} 更186"]
    assert [(p["key"], p["season"], p["videos"]) for p in dp["parts"]] == [("d116", 2, 1), ("d115", 1, 1), ("loose", 1, 1)]
    assert any("「第一季」" in x for x in dp["reasons"])

    assert units["星际穿越 Interstellar 2014 4K"]["kind"] == "movie" and units["星际穿越 Interstellar 2014 4K"]["type"] == "movie"
    up = units["Up.2009.1080p"]
    assert up["kind"] == "movie_file" and up["id"] == "f81" and "沒有自己的資料夾" in up["reasons"][0] and up["parent"] == "/影視/電影"

    assert [u["name"] for u in listing(c, h, kind="movie")["items"]] == ["Old Movie (2001)", "Up.2009.1080p", "星际穿越 Interstellar 2014 4K"]
    assert [u["name"] for u in listing(c, h, q="凡人")["items"]] == [FANREN]

    # 「集號不對的劇」：資料夾不規範的標出來，網頁改成整個資料夾整理
    rows = c.get("/web/api/moviepilot/reorganize", headers=h).json()["items"]
    assert any(it["name"] == FANREN and it["folder_nonstandard"] for it in rows)


def test_preview_whole_folder_and_checks(tmp_path: Path):
    app, fake, mp, media, c, h = setup(tmp_path)
    units = {u["id"]: u for u in listing(c, h)["items"]}
    mp.calls.clear()
    pv = preview(c, h, units["d110"]).json()
    sent = [b for p_, b in mp.calls if p_ == "/api/v1/transfer/manual"]
    # 整個資料夾一個項目，季明確給（名稱裡的「预计第二季度」不會被當成第 2 季），整理到同一層
    assert len(sent) == 1 and "fileitems" not in sent[0]
    item = sent[0]["fileitem"]
    assert (item["type"], item["fileid"], item["path"]) == ("dir", "110", f"/影視/劇集/{FANREN}/")
    assert (sent[0]["season"], sent[0]["media_id"], sent[0]["type_name"], sent[0]["target_path"], sent[0]["preview"]) == \
        (1, "106449", "电视剧", "/影視/劇集", True)
    assert pv["token"] and pv["summary"] == {"total": 3, "ok": 3, "failed": 0, "warnings": 0}
    assert pv["folders"] == ["凡人修仙传 (2020) {tmdbid=106449}"]
    assert pv["items"][0]["target"] == "/影視/劇集/凡人修仙传 (2020) {tmdbid=106449}/Season 1/凡人修仙传 - S01E01 - 第 1 集.mp4"

    # 併進旁邊照格式命名的資料夾：新位置在那裡面，沒有提醒
    kx = preview(c, h, units["d111"]).json()
    assert kx["summary"]["ok"] == 2 and not any(i["warnings"] for i in kx["items"])
    # 命名格式組出來的名稱不一樣：提醒不會併進去
    wrong = preview(c, h, units["d111"], tmdbid="106449").json()
    assert all("不會併進" in i["warnings"][0] for i in wrong["items"])

    # 有季資料夾的：一季一次，直接放著的影片照 115 上的檔名送
    mp.calls.clear()
    dp = preview(c, h, units["d114"], seasons={"d116": 2, "d115": 1, "loose": 0}).json()
    sent = [b for p_, b in mp.calls if p_ == "/api/v1/transfer/manual"]
    assert [(b["fileitem"]["fileid"], b["fileitem"]["type"], b["season"]) for b in sent] == \
        [("116", "dir", 2), ("115", "dir", 1), ("62", "file", 0)]  # 只有一支直接放著的影片：照它在 115 上的檔名送
    assert sent[2]["fileitem"]["name"] == "特别篇.mp4"
    assert dp["summary"]["ok"] == 2 and dp["summary"]["failed"] == 1  # 特别篇 認不出集號

    # 新位置和原本一樣、或在同步目錄外：那一部分整個不送
    mp.same.add("2.mp4")
    blocked = preview(c, h, units["d110"]).json()
    assert blocked["token"] is None and blocked["summary"]["ok"] == 0
    assert "新位置和原本一樣" in blocked["notes"][0] and "整個資料夾一起整理" in blocked["notes"][0]
    mp.same.clear()
    mp.outside.add("1.mp4")
    assert preview(c, h, units["d110"]).json()["token"] is None
    mp.outside.clear()

    # 電影：沒有自己資料夾的只送那一支
    mp.calls.clear()
    up = preview(c, h, units["f81"], tmdbid="14160").json()
    sent = [b for p_, b in mp.calls if p_ == "/api/v1/transfer/manual"]
    assert sent[0]["fileitem"]["type"] == "file" and sent[0]["fileitem"]["name"] == "Up.2009.1080p.mkv" and "season" not in sent[0]
    assert up["items"][0]["target"] == "/影視/電影/飞屋环游记 (2009) {tmdbid=14160}/飞屋环游记 (2009).mkv"

    # 參數檢查
    assert preview(c, h, units["d110"], type="").status_code == 400
    assert preview(c, h, units["d110"], tmdbid="abc").status_code == 400
    assert preview(c, h, {"id": "d999", "tmdbid": "", "type": "tv", "parts": []}).status_code == 400


def test_execute_moves_and_cleans_up(tmp_path: Path):
    app, fake, mp, media, c, h = setup(tmp_path)
    units = {u["id"]: u for u in listing(c, h)["items"]}
    fr, kx = preview(c, h, units["d110"]).json(), preview(c, h, units["d111"]).json()
    cleanup = [{"cid": 110, "path": units["d110"]["path"]}, {"cid": 111, "path": units["d111"]["path"]}]
    assert c.post(EXECUTE, json={"tokens": [fr["token"]], "cleanup": [{"cid": 114, "path": "/x"}]}, headers=h).status_code == 400
    assert c.post(EXECUTE, json={"tokens": [fr["token"], kx["token"]], "cleanup": cleanup}, headers=h).status_code == 200
    wait(lambda: not app.state.reorganizer.job.running)
    job = app.state.reorganizer.job
    assert (job.total, job.done, job.failed) == (5, 4, 1) and not job.errors  # 康熙 EP02 目標已經有，跳過
    folders = {i["name"]: i for i in job.items if i["state"] in ("kept", "removed")}
    assert folders[units["d110"]["path"]]["state"] == "removed" and "110" in fake.deleted
    assert folders[units["d111"]["path"]]["state"] == "kept" and "1 支影片" in folders[units["d111"]["path"]]["message"]
    assert {f["n"] for f in fake.files if f["cid"] == 113} == {"康熙来了 - S01E01 - 第 1 集.mp4", "康熙来了 - S01E02 - 第 2 集.mp4"}

    # 增量同步把 strm 搬到新位置，重新掃描後凡人修仙传不再列出，康熙来了（還剩 EP02）還在
    wait(lambda: not app.state.strm_sync.result.running and app.state.strm_sync.result.finished)
    assert (media / "劇集/凡人修仙传 (2020) {tmdbid=106449}/Season 1/凡人修仙传 - S01E03 - 第 3 集.strm").exists()
    app.state.scanner.scan_all()
    names = {u["name"] for u in listing(c, h, refresh="1")["items"]}
    assert FANREN not in names and "康熙来了 (2004)" in names


def test_cleanup_skips_folder_moviepilot_already_removed(tmp_path: Path):
    app, fake, mp, media, c, h = setup(tmp_path)
    units = {u["id"]: u for u in listing(c, h)["items"]}
    pv = preview(c, h, units["d110"]).json()
    mp.delete_source = True
    c.post(EXECUTE, json={"tokens": [pv["token"]], "cleanup": [{"cid": 110, "path": units["d110"]["path"]}]}, headers=h)
    wait(lambda: not app.state.reorganizer.job.running)
    kept = [i for i in app.state.reorganizer.job.items if i["state"] == "kept"]
    assert kept and "已經不在原本的位置" in kept[0]["message"] and "110" not in fake.deleted


def test_formats_fall_back_without_moviepilot_login(tmp_path: Path):
    app, fake, mp, media, c, h = setup(tmp_path, login=False)
    r = listing(c, h)
    assert r["formats"]["source"] == "default" and "帳號密碼" in r["formats"]["note"] and not r["ready"]["login"]
    # 預設格式沒有 tmdbid：名稱帶 tmdbid 標記的標出來；康熙来了 (2004) 名稱照格式，但影片沒放進季資料夾
    units = {u["name"]: u for u in r["items"]}
    assert "tmdbid 標記" in units["康熙来了 (2004) {tmdbid=6836}"]["reasons"][0]
    assert units["康熙来了 (2004)"]["reasons"][0].startswith("2 支影片直接放在劇集資料夾裡")
    assert "流浪 (2019) {tmdbid=9}" in units and "Dark" in units
    assert preview(c, h, units[FANREN]).status_code == 400  # 手動整理要帳號登入
