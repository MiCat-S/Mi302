"""片頭片尾：從播放進度學，給播放器 Emby 章節標記和 Jellyfin Intro Skipper 格式。"""

from pathlib import Path

from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.intro import TICK

RUNTIME = 40 * 60 * TICK
EP_NFO = "<episodedetails><title>第 {n} 集</title><season>1</season><episode>{n}</episode><runtime>40</runtime></episodedetails>"


def build(tmp_path: Path, **server):
    show = tmp_path / "tv" / "Show (2020)"
    show.mkdir(parents=True)
    for n in (1, 2, 3):
        (show / f"S01E0{n}.strm").write_text("http://x/a.mkv")
        (show / f"S01E0{n}.nfo").write_text(EP_NFO.format(n=n), encoding="utf-8")
    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data"), **server},
        "users": [{"name": "admin", "password": "pw", "admin": True}, {"name": "kid", "password": "pw"}],
        "libraries": [{"name": "劇集", "type": "tvshows", "paths": [str(tmp_path / "tv")]}],
    }), scan_on_start=False)
    app.state.scanner.scan_all()
    c = TestClient(app)
    return app, c


class Player:
    """模擬播放器：clock 是 Mi302 那邊的單調時鐘，我們自己往前撥。"""

    def __init__(self, app, c, user="admin"):
        self.app, self.c = app, c
        self.h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": user, "Pw": "pw"}).json()["AccessToken"]}
        self.now = 1000.0
        app.state.intro.clock = lambda: self.now

    def progress(self, item_id, seconds, after=10.0, stopped=False):
        self.now += after
        path = "/Sessions/Playing/Stopped" if stopped else "/Sessions/Playing/Progress"
        assert self.c.post(path, json={"ItemId": item_id, "PositionTicks": int(seconds * TICK)}, headers=self.h).status_code == 204


def episodes(c, h):
    series = c.get("/Items", params={"IncludeItemTypes": "Series", "Recursive": "true"}, headers=h).json()["Items"][0]
    return [e["Id"] for e in c.get(f"/Shows/{series['Id']}/Episodes", headers=h).json()["Items"]]


def markers(c, h, item_id):
    return {ch["MarkerType"]: ch["StartPositionTicks"] / TICK for ch in c.get(f"/Items/{item_id}", headers=h).json().get("Chapters", [])
            if ch["MarkerType"] != "Chapter"}


def test_learns_intro_from_a_skip_and_applies_to_the_season(tmp_path: Path):
    app, c = build(tmp_path)
    p = Player(app, c)
    e1, e2, e3 = episodes(c, p.h)
    assert markers(c, p.h, e1) == {}
    p.progress(e1, 0, after=0)
    p.progress(e1, 10)  # 正常播 10 秒
    p.progress(e1, 100)  # 10 秒內從 10 秒跳到 100 秒：片頭
    p.progress(e1, 110)
    assert markers(c, p.h, e1) == {"IntroStart": 10, "IntroEnd": 100}
    assert markers(c, p.h, e2) == {"IntroStart": 10, "IntroEnd": 100}  # 同一季其他集套用

    # 倍速播放不算跳：20 秒走了 40 秒
    p.progress(e2, 0, after=0)
    p.progress(e2, 40, after=20)
    assert markers(c, p.h, e2) == {"IntroStart": 10, "IntroEnd": 100}

    # 片尾：離結尾 90 秒時停下（切下一集）
    p.progress(e1, 2300)
    p.progress(e1, 2310, stopped=True)
    m = markers(c, p.h, e1)
    assert m["CreditsStart"] == 2310
    assert markers(c, p.h, e3)["CreditsStart"] == 2310  # 同樣片長，按「距離結尾」套用
    # 播完了不算片尾
    p.progress(e3, 2390, after=0)
    p.progress(e3, 2395, stopped=True)
    assert markers(c, p.h, e3)["CreditsStart"] == 2310

    # Jellyfin Intro Skipper 和 MediaSegments 的格式
    ts = c.get(f"/Episode/{e2}/IntroTimestamps", headers=p.h).json()
    assert (ts["Valid"], ts["IntroStart"], ts["IntroEnd"], ts["HideSkipPromptAt"]) == (True, 10.0, 100.0, 20.0)
    both = c.get(f"/Episode/{e2}/Timestamps", headers=p.h).json()
    assert both["Credits"]["IntroStart"] == 2310.0 and both["Credits"]["IntroEnd"] == 2400.0
    seg = c.get(f"/MediaSegments/{e2}", headers=p.h).json()["Items"]
    assert [(s["Type"], s["StartTicks"] / TICK) for s in seg] == [("Intro", 10), ("Outro", 2310)]
    assert "Chapters" in c.get("/Items", params={"Ids": e2, "Fields": "Chapters"}, headers=p.h).json()["Items"][0]

    st = c.get("/web/api/intro/status", headers=p.h).json()
    assert (st["seasons"], st["episodes"]) == (1, 1) and st["recent"][0]["intro"] == [10, 100] and st["recent"][0]["credits_tail"] == 90
    assert c.post("/web/api/intro/clear", json={}, headers=p.h).json()["removed"] == 2
    assert markers(c, p.h, e2) == {} and c.get(f"/Episode/{e2}/IntroTimestamps", headers=p.h).status_code == 404


def test_median_across_users_and_own_marks_win(tmp_path: Path):
    app, c = build(tmp_path)
    a, b = Player(app, c, "admin"), Player(app, c, "kid")
    e1, e2, e3 = episodes(c, a.h)
    for player, end in ((a, 90), (b, 110)):
        player.progress(e1, 0, after=0)
        player.progress(e1, 5)
        player.progress(e1, end)
    assert markers(c, a.h, e1)["IntroEnd"] == 100  # 兩個人取中位數
    a.progress(e2, 0, after=0)
    a.progress(e2, 30)
    a.progress(e2, 150)
    assert markers(c, a.h, e2) == {"IntroStart": 30, "IntroEnd": 150}  # 自己有紀錄的用自己的
    assert markers(c, a.h, e3)["IntroEnd"] == 110  # 沒紀錄的用整季中位數（90、110、150）


def test_disabled_learns_and_serves_nothing(tmp_path: Path):
    app, c = build(tmp_path, intro_skip=False)
    p = Player(app, c)
    e1, *_ = episodes(c, p.h)
    p.progress(e1, 0, after=0)
    p.progress(e1, 5)
    p.progress(e1, 100)
    assert markers(c, p.h, e1) == {} and app.state.db.one("SELECT COUNT(*) AS c FROM intro_obs")["c"] == 0
    assert c.get(f"/Episode/{e1}/IntroTimestamps", headers=p.h).status_code == 404
