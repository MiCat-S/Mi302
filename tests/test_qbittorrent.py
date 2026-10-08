"""qBittorrent：下載中的種子太久沒速度就刪掉，排在後面的接著開始。"""

from pathlib import Path
from urllib.parse import parse_qsl

import httpx

from embyserver.config import QBittorrentConfig
from embyserver.qbittorrent import QBittorrent

from fakes import admin_headers, make_client


class FakeQB:
    """WebUI API 的假 qBittorrent：password 有填就要先登入；old = 5.0 以前（開始叫 resume，沒有 start）。"""

    def __init__(self, torrents, password="", old=False):
        self.torrents = {t["hash"]: dict(t) for t in torrents}
        self.password, self.old = password, old
        self.calls = []
        self.offline = False
        self.trackers = {}  # hash → tracker status（沒寫的是 2 = 正常）

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("refused", request=request)
        path = request.url.path
        form = dict(parse_qsl(request.content.decode())) if request.method == "POST" else {}
        self.calls.append((path, form))
        if path == "/api/v2/auth/login":
            if form.get("password") != self.password:
                return httpx.Response(200, text="Fails.")
            return httpx.Response(200, text="Ok.", headers={"set-cookie": "SID=s1; path=/"})
        if self.password and "SID=s1" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text="Forbidden")
        if path == "/api/v2/app/version":
            return httpx.Response(200, text="v4.6.7" if self.old else "v5.2.2")
        if path == "/api/v2/torrents/info":
            return httpx.Response(200, json=list(self.torrents.values()))
        if path == "/api/v2/torrents/trackers":
            h = request.url.params["hash"]
            return httpx.Response(200, json=[{"url": "** [DHT] **", "status": 2}, {"url": "https://t/announce", "status": self.trackers.get(h, 2)}])
        if path == "/api/v2/torrents/setForceStart":
            for h in form["hashes"].split("|"):
                self.torrents[h]["state"] = "forcedDL" if form["value"] == "true" else "queuedDL"
            return httpx.Response(200)
        if path == "/api/v2/torrents/delete":
            for h in form["hashes"].split("|"):
                del self.torrents[h]
            return httpx.Response(200)
        if path == ("/api/v2/torrents/resume" if self.old else "/api/v2/torrents/start"):
            for h in form["hashes"].split("|"):
                self.torrents[h]["state"] = "queuedDL"
            return httpx.Response(200)
        return httpx.Response(404, text="Not Found")


def torrent(h, state, progress=0.0, downloaded=0, priority=0, added_on=0):
    return {"hash": h, "name": h.upper(), "state": state, "progress": progress, "downloaded": downloaded,
            "priority": priority, "added_on": added_on, "size": 1000, "category": "日番", "num_seeds": 0, "num_complete": -1}


def test_stalled_torrents_are_removed_and_next_ones_start():
    fake = FakeQB([
        torrent("dead", "forcedDL", priority=1),  # 一直沒速度；強制開始的不佔佇列名額，刪了不會空出位子
        torrent("trickle", "downloading", 0.2, priority=2),  # 每 5 分鐘 100 KB，平均不到 1 KB/s
        torrent("fast", "downloading", 0.5, priority=3),
        torrent("seed", "stalledUP", 1.0),  # 做種的不管
        torrent("paused", "stoppedDL", priority=4),
        torrent("late", "queuedDL", priority=5),  # 排隊很久，中途才輪到
        torrent("tail", "stoppedDL", priority=6),
    ])
    cfg = QBittorrentConfig(url="http://qb:8080", remove_stalled=True, stalled_minutes=60, stalled_speed=1)
    qb = QBittorrent(cfg, transport=httpx.MockTransport(fake))
    now = [1_000_000.0 - 300]
    qb._clock = lambda: now[0]

    def tick():  # 過 5 分鐘再看一次
        now[0] += 300
        for h, got in (("trickle", 100 * 1024), ("fast", 5 * 1024 * 1024)):
            if h in fake.torrents:
                fake.torrents[h]["downloaded"] += got
        return qb.check()

    for step in range(13):  # 0、5、…、60 分鐘
        if step == 6:
            fake.torrents["late"]["state"] = "stalledDL"  # 第 30 分鐘才開始下載，從這時候才算
        last = tick()
        if step < 12:
            assert not any(path.endswith("/delete") for path, _ in fake.calls), step
    assert [(r["hash"], r["minutes"], r["files"]) for r in last.removed] == [("dead", 60, True), ("trickle", 60, True)]
    deletes = [form for path, form in fake.calls if path.endswith("/delete")]
    assert deletes == [{"hashes": "dead|trickle", "deleteFiles": "true"}]
    # 刪兩個只空出一個名額（dead 是強制開始的），接著開始一個：照佇列順序 late 已經在下載了，輪到的是 paused
    assert [s["hash"] for s in last.started] == ["paused"]
    assert [form for path, form in fake.calls if path.endswith("/start")] == [{"hashes": "paused"}]
    assert set(fake.torrents) == {"fast", "seed", "paused", "late", "tail"}
    assert [(s["hash"], s["quiet"]) for s in last.slow] == [("late", 1800)]
    assert last.downloading == 2 and qb.removed()[0]["name"] == "DEAD"

    # 連不上的那段時間不算：連回來之後從頭算，不會一連上就刪
    fake.offline = True
    assert "拒絕連線" in tick().error
    fake.offline = False
    for _ in range(7):
        last = tick()
    assert last.error == "" and last.removed == [] and last.slow[0]["quiet"] == 1800

    # 關掉自動刪除：照樣記沒速度多久，但不刪
    cfg.remove_stalled, cfg.delete_files = False, False
    for _ in range(6):
        last = tick()
    assert last.removed == [] and last.slow[0]["quiet"] == 3600 and "late" in fake.torrents
    # 再打開，但只刪做種數為 0 的：tracker 說還有人做種就先不刪、只列出來；做種數掉到 0 才刪（只拿掉種子、檔案留著）
    cfg.remove_stalled, cfg.no_seeds_only = True, True
    fake.torrents["late"]["num_complete"] = 1
    last = tick()
    assert last.removed == [] and last.slow[0]["seeds"] == 1 and "late" in fake.torrents
    fake.torrents["late"]["num_complete"] = -1  # tracker 沒回報，當成 0
    assert [(r["hash"], r["files"], r["seeds"]) for r in tick().removed] == [("late", False, 0)]
    assert [form for path, form in fake.calls if path.endswith("/delete")][-1] == {"hashes": "late", "deleteFiles": "false"}


def test_keep_active_force_starts_and_judges_quickly():
    fake = FakeQB([
        torrent("live", "downloading", 0.3),
        torrent("stuck", "stalledDL", 0.1),  # qB 自己開的、沒速度：照 60 分鐘的規則，不在這裡管
        torrent("dead", "queuedDL", priority=1),  # 強制開始後還是沒速度
        torrent("good", "queuedDL", priority=2),  # 強制開始後有速度
        torrent("notracker", "stoppedDL", priority=3),  # tracker 沒回應：不是種子的錯
        torrent("seeded", "queuedDL", priority=4),  # 還有人做種（no_seeds_only）
        torrent("last", "queuedDL", priority=5),
    ])
    fake.trackers["notracker"] = 4
    fake.torrents["stuck"]["num_complete"] = 2  # 還有人做種：60 分鐘的規則不會刪它，這裡只看強制開始的那一套
    cfg = QBittorrentConfig(url="http://qb:8080", remove_stalled=True, keep_active=2, force_seconds=30, no_seeds_only=True)
    qb = QBittorrent(cfg, transport=httpx.MockTransport(fake))
    now = [100.0]
    qb._clock = lambda: now[0]
    forced = lambda: [form for path, form in fake.calls if path.endswith("/setForceStart")]  # noqa: E731

    def tick(seconds):
        now[0] += seconds
        fake.torrents["live"]["downloaded"] += 1 << 20
        if fake.torrents.get("good", {}).get("state") == "forcedDL":
            fake.torrents["good"]["downloaded"] += 1 << 20
        return qb.check()

    fake.torrents["live"]["dlspeed"] = 1 << 20
    last = tick(0)  # 第一次看：live 這一刻有速度、stuck 沒有：差一個，照佇列順序強制開始 dead
    fake.torrents["live"]["dlspeed"] = 0  # 之後靠「上一輪到現在有下載到東西」判斷
    assert [s["hash"] for s in last.started] == ["dead"] and forced() == [{"hashes": "dead", "value": "true"}]
    assert last.forcing == 1 and last.moving == 1 and fake.torrents["dead"]["state"] == "forcedDL"
    last = tick(15)  # 還沒到 30 秒：等
    assert last.removed == [] and last.started == [] and last.forcing == 1 and last.slow[-1]["forced"]
    last = tick(15)  # 30 秒沒速度、tracker 正常：刪掉，再強制開始 good
    assert [(r["hash"], r["rule"], r["seconds"]) for r in last.removed] == [("dead", "forced", 30)]
    assert [s["hash"] for s in last.started] == ["good"] and "dead" not in fake.torrents
    last = tick(30)  # good 有速度了：夠兩個，不再開
    assert last.removed == [] and last.started == [] and last.forcing == 0 and last.moving == 2
    fake.torrents["live"]["state"], fake.torrents["live"]["progress"] = "stalledUP", 1.0  # live 下載完了：又差一個 → notracker
    last = tick(300)
    assert [s["hash"] for s in last.started] == ["notracker"]
    last = tick(30)  # tracker 沒回應：不刪，放回佇列，接著試 seeded
    assert last.removed == [] and [(t["hash"], t["why"]) for t in last.skipped] == [("notracker", "tracker 沒回應")]
    assert fake.torrents["notracker"]["state"] == "queuedDL" and [s["hash"] for s in last.started] == ["seeded"]
    fake.torrents["seeded"]["num_complete"] = 3
    last = tick(30)  # 還有人做種：不刪，放回佇列，接著試 last
    assert [(t["hash"], t["why"]) for t in last.skipped] == [("seeded", "還有 3 人做種")] and [s["hash"] for s in last.started] == ["last"]
    last = tick(30)  # last 也沒速度、tracker 正常：刪；排隊的只剩試過的，不再開
    assert [r["hash"] for r in last.removed] == ["last"] and last.started == [] and last.forcing == 0
    for _ in range(11):  # 一小時內：試過的不再試，排隊的沒別的了
        assert tick(300).started == []
    last = tick(300)  # 滿一小時：試過的可以再試，還是差一個 → notracker 再來
    assert [s["hash"] for s in last.started] == ["notracker"]
    cfg.keep_active = 0  # 關掉：不再強制，等的也不管了
    assert tick(30).forcing == 0 and fake.torrents["notracker"]["state"] == "forcedDL"


def test_login_old_versions_and_web_api(tmp_path: Path):
    fake = FakeQB([torrent("dead", "stalledDL"), torrent("next", "pausedDL", priority=2)], password="pw", old=True)
    cfg = QBittorrentConfig(url="http://qb:8080", username="admin", password="wrong")
    qb = QBittorrent(cfg, transport=httpx.MockTransport(fake))
    assert qb.test() == {"ok": False, "message": "qBittorrent 帳號或密碼不對（HTTP 200）"}
    # 同一組帳密不再撞：qBittorrent 連續失敗幾次就封 IP，MoviePilot 從同一台連的話會一起被擋
    logins = lambda: sum(1 for path, _ in fake.calls if path.endswith("/auth/login"))  # noqa: E731
    n = logins()
    assert "分鐘內不再試" in qb.test()["message"] and logins() == n
    cfg.password = "pw"  # 改了密碼：馬上換一個新的連線重新登入
    r = qb.test()
    assert r["ok"] and "v4.6.7" in r["message"] and "下載中 1 個，排在後面等著的 1 個" in r["message"]
    # 5.0 以前沒有 start：找不到就改叫 resume
    cfg.remove_stalled, cfg.stalled_minutes = True, 10
    now = [0.0]
    qb._clock = lambda: now[0]
    for _ in range(3):
        now[0] += 300
        last = qb.check()
    assert [s["hash"] for s in last.started] == ["next"] and fake.torrents["next"]["state"] == "queuedDL"
    assert [p for p, _ in fake.calls][-3:] == ["/api/v2/torrents/delete", "/api/v2/torrents/start", "/api/v2/torrents/resume"]

    c = make_client(tmp_path, {"users": [{"name": "admin", "password": "pw", "admin": True}]})
    h = admin_headers(c)
    assert c.post("/web/api/qbittorrent/check", headers=h).status_code == 400  # 還沒填網址
    body = {"qbittorrent": {"url": "http://qb:8080/", "username": "admin", "password": "pw", "remove_stalled": True,
                            "stalled_minutes": "1", "stalled_speed": "-5"}}
    saved = c.put("/web/api/settings", json=body, headers=h).json()["qbittorrent"]
    assert saved["url"] == "http://qb:8080" and saved["stalled_minutes"] == 10 and saved["stalled_speed"] == 0
    assert c.put("/web/api/settings", json={"qbittorrent": {"url": "qb:8080"}}, headers=h).status_code == 400
    live = c.app.state.qbittorrent
    assert live.cfg.url == "http://qb:8080" and live.cfg.remove_stalled
    live._transport = httpx.MockTransport(FakeQB([torrent("a", "downloading")], password="pw"))
    live.close()
    s = c.post("/web/api/qbittorrent/check", headers=h).json()
    assert s["enabled"] and s["active"] and s["downloading"] == 1 and s["error"] == "" and s["history"] == []
    assert c.get("/web/api/qbittorrent/status", headers=h).json()["at"] == s["at"]
