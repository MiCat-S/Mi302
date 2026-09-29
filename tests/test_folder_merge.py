"""合併重複的資料夾：比對名稱、依同步紀錄找成對的資料夾、交給 MoviePilot 整理進帶 tmdbid 的那個、清掉舊資料夾
（假 115 + 假 MoviePilot）。"""

import json
import posixpath
from pathlib import Path

import httpx

from embyserver.folder_merge import folder_key, folder_tag
from embyserver.strm_sync import FULL

from test_incremental import T0
from test_reorganize import FakeMP, build, wait

EXECUTE = "/web/api/moviepilot/reorganize/execute"


def test_folder_key_and_tag():
    assert folder_tag("康熙来了 (2004) {tmdbid=6836}") == "6836"
    assert folder_tag("Dark [tmdb=70523]") == "70523" and folder_tag("Dark {tmdb-70523}") == "70523"
    assert folder_tag("康熙来了 (2004)") == "" and folder_tag("2004 {tmdbid=}") == ""
    same = {folder_key(n) for n in ["康熙来了 (2004) {tmdbid=6836}", "康熙来了 (2004)", "康熙来了（2004）", "康熙来了(2004)",
                                    "康熙来了  (2004) "]}
    assert same == {"康熙来了 (2004)"}
    assert folder_key("Dark [tmdb=70523]") == folder_key("dark") == "dark"
    assert folder_key("康熙来了 (2004)") != folder_key("康熙来了 (2015)")  # 年份不同是不同的劇（重拍）


class MergeMP(FakeMP):
    """MoviePilot 的命名設定是「劇名 (年份) {tmdbid=編號}/Season 1/劇名 - S01E01」：照它放進帶 tmdbid 的資料夾；
    目標已經有同一集就失敗。shows：tmdbid → (資料夾名, 劇名, Season 1 的 115 目錄 id)。"""

    def __init__(self, fake115, shows):
        super().__init__(fake115)
        self.shows = shows

    def handler(self, request: httpx.Request) -> httpx.Response:
        manual = request.url.path == "/api/v1/transfer/manual" and request.headers.get("Authorization") == "Bearer jwt"
        body = json.loads(request.content or b"{}") if manual else {}
        show = self.shows.get(body.get("tmdbid"))
        if not show:
            return super().handler(request)
        self.calls.append((request.url.path, body))
        folder, title, season_cid = show
        items = []
        for fi in body["fileitems"]:
            ep = self.episode(fi, body.get("episode_format"))
            if not ep:
                items.append({"source": fi["path"], "success": False, "message": "无法识别集数", "state": "failed"})
                continue
            target = f"{body['target_path']}/{folder}/Season 1/{title} - S01E{ep:02d}.mp4"
            if any(f["cid"] == season_cid and f["n"] == posixpath.basename(target) for f in self.fake115.files):
                items.append({"source": fi["path"], "success": False, "message": "目标文件已存在", "state": "failed"})
                continue
            item = {"source": fi["path"], "target": target, "success": True, "episode": ep, "season": 1}
            if not body["preview"]:
                self.fake115.file(int(fi["fileid"])).update(cid=season_cid, n=posixpath.basename(target))
                self.fake115.event(6, int(fi["fileid"]))
                item["state"] = "completed"
            items.append(item)
        return httpx.Response(200, json={"success": True, "data": {"items": items}})


def setup(tmp_path: Path):
    app, fake, _, media, c, h = build(tmp_path)
    fake.dirs.update({
        110: ("康熙来了 (2004)", 102), 111: ("康熙来了 (2004) {tmdbid=6836}", 102), 112: ("Season 1", 111),
        120: ("大学生了没 (2007)", 102), 121: ("大学生了没 (2007) {tmdbid=7777}", 102), 122: ("Season 1", 121),
        130: ("流浪地球 (2019)", 101), 131: ("流浪地球 (2019) {tmdbid=535167}", 101),
        140: ("X (2000)", 102), 141: ("X (2000) {tmdbid=1}", 102), 142: ("X (2000) {tmdbid=2}", 102),
        150: ("空的 (2001)", 102), 151: ("空的 (2001) {tmdbid=3}", 102),
    })
    files = [(70, 110, "康熙来了 EP01.mp4"), (71, 110, "康熙来了 EP02.mp4"), (72, 110, "康熙来了 EP03.mp4"), (73, 110, "花絮.mp4"),
             (74, 112, "康熙来了 - S01E03.mp4"), (75, 110, "poster.jpg"),
             (80, 120, "大学生了没 EP01.mp4"), (81, 122, "大学生了没 - S01E05.mp4"),
             (82, 130, "流浪地球.mkv"), (83, 131, "流浪地球 (2019).mkv"), (84, 140, "x.mp4"), (85, 141, "x.mp4"), (86, 142, "x.mp4")]
    for i, (fid, cid, name) in enumerate(files):
        fake.files.append({"fid": fid, "cid": cid, "n": name, "pc": f"pc{fid}".ljust(17, "x"), "s": 800_000_000, "te": T0 + i})
    assert not app.state.strm_sync.run(FULL).errors
    mp = MergeMP(fake, {6836: ("康熙来了 (2004) {tmdbid=6836}", "康熙来了", 112), 7777: ("大学生了没 (2007) {tmdbid=7777}", "大学生了没", 122)})
    app.state.moviepilot._transport = httpx.MockTransport(mp.handler)
    return app, fake, mp, media, c, h


def preview(c, h, mp, group, **extra):
    src, tgt = group["sources"][0], group["targets"][0]
    plan = c.get("/web/api/moviepilot/reorganize/folder", params={"cid": src["cid"], "path": src["path"]}, headers=h).json()
    body = {"plan_id": plan["plan_id"], "tmdbid": tgt["tmdbid"], "type": group["type"], "target": "parent", "scrape": False,
            "expect_dir": tgt["path"], "groups": [{"key": g["key"], "template": g["template"], "enabled": True} for g in plan["groups"]],
            **extra}
    return c.post("/web/api/moviepilot/reorganize/preview", json=body, headers=h).json()


def test_find_pairs_preview_execute_and_cleanup(tmp_path: Path):
    app, fake, mp, media, c, h = setup(tmp_path)
    assert c.get("/web/api/115/duplicate-folders").status_code == 401
    r = c.get("/web/api/115/duplicate-folders", headers=h).json()
    assert r["ready"] == {"moviepilot": True, "login": True, "p115": True}
    groups = {g["sources"][0]["name"]: g for g in r["groups"]}
    # 只有一個資料夾的劇（Dark、中国新说唱）不算；沒有影片的資料夾（空的）不在同步紀錄裡，不會列；照 115 路徑排
    assert list(groups) == ["X (2000)", "大学生了没 (2007)", "康熙来了 (2004)", "流浪地球 (2019)"]
    k = groups["康熙来了 (2004)"]
    assert (k["parent"], k["type"], k["videos"]) == ("/影視/劇集", "tv", 4)  # poster.jpg 不算影片；類型看媒體庫是劇集
    assert k["sources"] == [{"cid": 110, "name": "康熙来了 (2004)", "path": "/影視/劇集/康熙来了 (2004)", "tmdbid": "", "videos": 4}]
    assert k["targets"] == [{"cid": 111, "name": "康熙来了 (2004) {tmdbid=6836}", "path": "/影視/劇集/康熙来了 (2004) {tmdbid=6836}",
                             "tmdbid": "6836", "videos": 1}]
    assert groups["流浪地球 (2019)"]["type"] == "auto" and groups["流浪地球 (2019)"]["parent"] == "/影視/電影"  # 不在任何媒體庫裡
    assert sorted(t["tmdbid"] for t in groups["X (2000)"]["targets"]) == ["1", "2"]  # 兩個帶 tmdbid 的：網頁要選

    # 預覽：用帶 tmdbid 那個的編號、整理到同一層；目標已有的第 3 集和認不出集號的不會送
    pv = preview(c, h, mp, k)
    sent = [b for p_, b in mp.calls if p_ == "/api/v1/transfer/manual"]
    assert sent and all(b["tmdbid"] == 6836 and b["type_name"] == "电视剧" and b["target_path"] == "/影視/劇集" for b in sent)
    items = {i["name"]: i for i in pv["items"]}
    assert items["康熙来了 EP01.mp4"]["ok"] and not items["康熙来了 EP01.mp4"]["warnings"]
    assert items["康熙来了 EP01.mp4"]["target"] == "/影視/劇集/康熙来了 (2004) {tmdbid=6836}/Season 1/康熙来了 - S01E01.mp4"
    assert not items["康熙来了 EP03.mp4"]["ok"] and "已存在" in items["康熙来了 EP03.mp4"]["message"]
    assert not items["花絮.mp4"]["ok"]
    assert pv["summary"] == {"total": 4, "ok": 2, "failed": 2, "warnings": 0}
    # MoviePilot 的命名設定產生的資料夾名稱和帶 tmdbid 的資料夾不同：提醒
    mp.shows[6836] = ("康熙來了 (2004)", "康熙来了", 112)
    other = preview(c, h, mp, k)
    assert all("目標資料夾" in i["warnings"][0] for i in other["items"] if i["ok"]) and other["summary"]["warnings"] == 2
    mp.shows[6836] = ("康熙来了 (2004) {tmdbid=6836}", "康熙来了", 112)
    pv = preview(c, h, mp, k)
    d = groups["大学生了没 (2007)"]
    pv2 = preview(c, h, mp, d)
    assert pv2["summary"]["ok"] == 1

    # 只能清這次整理的來源資料夾
    bad = c.post(EXECUTE, json={"tokens": [pv["token"], pv2["token"]], "cleanup": [{"cid": 111, "path": k["targets"][0]["path"]}]}, headers=h)
    assert bad.status_code == 400 and "來源資料夾" in bad.text
    # 兩個預覽一起執行；整理完 115 上還有影片的資料夾保留，沒有影片的移到回收站
    cleanup = [{"cid": 110, "path": k["sources"][0]["path"]}, {"cid": 120, "path": d["sources"][0]["path"]}]
    assert c.post(EXECUTE, json={"tokens": [pv["token"], pv2["token"]], "cleanup": cleanup}, headers=h).status_code == 200
    wait(lambda: not app.state.reorganizer.job.running)
    job = app.state.reorganizer.job
    assert (job.title, job.total, job.done, job.failed) == ("合併 2 個資料夾", 3, 3, 0)
    folders = {i["name"]: i for i in job.items if i["state"] in ("kept", "removed")}
    assert folders["/影視/劇集/康熙来了 (2004)"]["state"] == "kept" and "2 支影片" in folders["/影視/劇集/康熙来了 (2004)"]["message"]
    assert folders["/影視/劇集/大学生了没 (2007)"]["state"] == "removed"
    assert fake.deleted[-1] == "120" and "110" not in fake.deleted  # 前面的是全量同步用完刪掉的目錄樹檔
    assert {f["n"] for f in fake.files if f["cid"] == 112} == {"康熙来了 - S01E01.mp4", "康熙来了 - S01E02.mp4", "康熙来了 - S01E03.mp4"}
    assert {f["n"] for f in fake.files if f["cid"] == 110} == {"康熙来了 EP03.mp4", "花絮.mp4", "poster.jpg"}
    # 預覽用過就作廢
    assert c.post(EXECUTE, json={"tokens": [pv["token"]]}, headers=h).status_code == 400
