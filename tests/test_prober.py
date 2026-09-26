"""媒體資訊探測：取直鏈、跑 ffprobe、寫出 X-mediainfo.json；限速、熔斷、各種失敗。"""

import json
import subprocess
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.mediainfo import sidecar_path
from embyserver.p115 import PLAIN_UA

from test_mediainfo import PROBE


def make(tmp_path: Path, logged_in=True, **mi):
    raw = {
        "server": {"data_dir": str(tmp_path / "data")},
        "users": [{"name": "admin", "password": "pw", "admin": True}],
        "libraries": [{"name": "電影", "type": "movies", "paths": [str(tmp_path / "movies")]}],
        # 用一個一定存在的執行檔假裝是 ffprobe；真正執行的是下面換掉的 runner
        "mediainfo": {"enabled": True, "ffprobe": sys.executable, "interval": 0.5, **mi},
    }
    if logged_in:
        raw["p115"] = {"cookies": "UID=1"}
    app = create_app(config_from_dict(raw), scan_on_start=False)
    prober = app.state.prober
    links, cmds = [], []
    app.state.p115._fetch_download_url = lambda pc, ua: links.append((pc, ua)) or f"https://cdn.115.test/{pc}?t=1"

    def runner(cmd, capture_output, timeout):
        cmds.append(cmd)
        url = cmd[-1]
        if "broken" in url:
            return subprocess.CompletedProcess(cmd, 1, b"", f"[https @ 0x1] HTTP error 403 Forbidden\n{url}: Server returned 403".encode())
        return subprocess.CompletedProcess(cmd, 0, json.dumps(PROBE).encode(), b"")

    prober.runner = runner
    return app, prober, links, cmds


def strm(tmp_path: Path, name: str, content: str, mtime: float = 0) -> Path:
    p = tmp_path / "movies" / name / f"{name}.strm"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    if mtime:
        import os
        os.utime(p, (mtime, mtime))
    return p


def test_probe_115_strm_writes_sidecar(tmp_path: Path):
    app, prober, links, cmds = make(tmp_path)
    movie = strm(tmp_path, "A (2020)", "http://mi302:8096/d/abcdefghijklmnopq.mkv")
    app.state.scanner.scan_all()
    r = prober.run(None, "manual")
    assert (r.total, r.done, r.failed) == (1, 1, 0)
    # 取直鏈和 ffprobe 用同一個 UA；重用連線
    assert links == [("abcdefghijklmnopq", PLAIN_UA)]
    cmd = cmds[0]
    assert cmd[cmd.index("-user_agent") + 1] == PLAIN_UA and "-multiple_requests" in cmd
    assert cmd[-1] == "https://cdn.115.test/abcdefghijklmnopq?t=1"
    side = json.loads(sidecar_path(movie).read_text())
    assert side[0]["MediaSourceInfo"]["MediaStreams"][0]["Width"] == 3840
    assert app.state.prober.store.get(str(movie))["source"]["Container"] == "mkv"
    row = app.state.db.one("SELECT runtime_ticks FROM items WHERE path=?", (str(movie),))
    assert row["runtime_ticks"] == 36005000000  # 片長也補上了
    assert prober.missing() == []  # 做過的不再做


def test_missing_order_skips_and_failures(tmp_path: Path):
    app, prober, links, cmds = make(tmp_path)
    old = strm(tmp_path, "Old", "http://cdn.example.com/old.mp4", mtime=time.time() - 3600)
    new = strm(tmp_path, "New", "http://cdn.example.com/new.mkv")
    done = strm(tmp_path, "Done", "http://cdn.example.com/done.mkv")
    sidecar_path(done).write_text("[]")  # 旁邊已經有 json 的不做
    empty = strm(tmp_path, "Empty", "")
    broken = strm(tmp_path, "Broken", "http://cdn.example.com/broken.mkv")
    todo = prober.missing()
    assert set(todo) == {str(new), str(empty), str(broken), str(old)}  # 旁邊已經有 json 的不做
    assert todo[-1] == str(old)  # 新的先做
    r = prober.run(None, "manual")
    assert (r.total, r.done, r.failed, r.skipped) == (4, 2, 1, 1)
    assert links == []  # 不是 115 的網址直接給 ffprobe
    err = next(e for e in r.errors if e.startswith("Broken"))
    assert "403" in err and "cdn.example.com" not in err  # 錯誤訊息抹掉網址


def test_breaker_and_login_abort_the_batch(tmp_path: Path):
    app, prober, links, cmds = make(tmp_path, concurrency=1)
    for i in range(3):
        strm(tmp_path, f"M{i}", f"http://mi302/d/{'abcdefghijklmnop' + str(i)}.mkv")
    app.state.p115.breaker.trip("HTTP 405")
    r = prober.run(None, "manual")
    assert (r.total, r.done, r.failed) == (3, 0, 3) and len(r.errors) == 1 and "限流" in r.errors[0]
    assert links == [] and cmds == []  # 一個直鏈都沒取

    app2, prober2, links2, _ = make(tmp_path / "b", logged_in=False)
    strm(tmp_path / "b", "X", "http://mi302/d/abcdefghijklmnopq.mkv")
    r = prober2.run(None, "manual")
    assert r.failed == 1 and "尚未登入 115" in r.errors[0] and links2 == []


def test_no_ffprobe_and_pacing(tmp_path: Path):
    app, prober, links, cmds = make(tmp_path, concurrency=3)
    for i in range(3):
        strm(tmp_path, f"M{i}", f"http://mi302/d/{'abcdefghijklmnop' + str(i)}.mkv")
    start = time.monotonic()
    assert prober.run(None, "manual").done == 3
    assert time.monotonic() - start >= 1.0  # 取直鏈每次至少隔 0.5 秒，同時跑也一樣

    prober.cfg.ffprobe = str(tmp_path / "no-such-ffprobe")
    r = prober.run(None, "manual")
    assert r.total == 0 and "找不到 ffprobe" in r.errors[0]


def test_endpoints_and_after_sync(tmp_path: Path):
    app, prober, links, cmds = make(tmp_path, enabled=False)
    strm(tmp_path, "A (2020)", "http://cdn.example.com/a.mkv")
    app.state.scanner.scan_all()
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    s = c.get("/web/api/mediainfo/status", headers=h).json()
    assert (s["enabled"], s["have"], s["total"], bool(s["ffprobe"])) == (False, 0, 1, True)
    assert c.post("/web/api/mediainfo/probe", headers=h).status_code == 400  # 沒開探測不跑

    assert c.put("/web/api/settings", json={"mediainfo": {"enabled": True, "concurrency": 9, "interval": 0.1}},
                 headers=h).status_code == 200
    mi = app.state.config.mediainfo
    assert (mi.enabled, mi.concurrency, mi.interval) == (True, 3, 0.5)  # 夾在 115 能接受的範圍
    from embyserver import config_file
    assert "  concurrency: 3" in config_file.render(app.state.config)

    assert c.post("/web/api/mediainfo/probe", headers=h).json()["started"]
    for _ in range(50):
        if not prober.result.running and prober.result.finished:
            break
        time.sleep(0.05)
    assert c.get("/web/api/mediainfo/status", headers=h).json()["have"] == 1

    # 同步產生新 strm 後自動探測
    calls = []
    prober.run_in_background = lambda paths, source: calls.append((paths, source)) or True
    from embyserver.strm_sync import SyncResult
    app.state.strm_sync.on_done(SyncResult(new_files=["/x/new.strm"]))
    assert calls == [(["/x/new.strm"], "sync")]
    mi.after_sync = False
    app.state.strm_sync.on_done(SyncResult(new_files=["/x/new2.strm"]))
    assert len(calls) == 1


def test_replaced_file_invalidates_media_info(tmp_path: Path):
    from embyserver.mediainfo import build_sidecar, write_sidecar
    from embyserver.strm_sync import FULL

    from test_incremental import Fake115
    from test_incremental import make as make_sync

    fake = Fake115()
    sync = make_sync(tmp_path, fake)
    sync.run(FULL)
    movie = tmp_path / "media" / "電影" / "Old Movie (2001).strm"
    write_sidecar(movie, build_sidecar(PROBE, str(movie)))
    sync.p115.db.execute("INSERT INTO media_info(path, data, mtime) VALUES(?, '{}', 1)", (str(movie),))

    # 只改伺服器網址：strm 重寫了，但還是同一個檔案，媒體資訊照用
    sync.cfg.base_url = "http://nas:8096"
    r = sync.run(FULL)
    assert r.strm_created == 2 and r.replaced == [] and sidecar_path(movie).exists()

    # 115 上的檔案被換掉（pickcode 變了）：舊的媒體資訊作廢，列入重新探測
    fake.file(1)["pc"] = "z" * 17
    r = sync.run(FULL)
    assert r.replaced == [str(movie)] and not sidecar_path(movie).exists()
    assert sync.p115.db.one("SELECT 1 FROM media_info WHERE path=?", (str(movie),)) is None
    assert r.as_dict()["replaced"] == 1


class FakeClock:
    """sleep 直接把時間往前推，不真的等。"""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_hourly_limit_waits_for_the_oldest_fetch(tmp_path: Path):
    app, prober, links, cmds = make(tmp_path, hourly_limit=3, concurrency=1)
    clock = FakeClock()
    prober.clock, prober.sleep = clock, clock.sleep
    times = []
    real = app.state.p115._fetch_download_url
    app.state.p115._fetch_download_url = lambda pc, ua: times.append(clock.now) or real(pc, ua)
    for i in range(5):
        strm(tmp_path, f"M{i}", f"http://mi302/d/{'abcdefghijklmnop' + str(i)}.mkv")
    assert prober.run(None, "manual").done == 5
    assert times[2] - times[0] >= 1.0  # 間隔照舊
    assert times[3] - times[0] >= 3600 and times[4] - times[1] >= 3600  # 第 4 次要等第 1 次滿一小時
    assert max(clock.slept) <= 60  # 每分鐘醒來看一次
    assert prober.usage()["used"] == 3 and prober.usage()["limit"] == 3  # 第 3、4、5 次都在這一小時內


def test_slowdown_after_breaker_recovers(tmp_path: Path):
    app, prober, links, cmds = make(tmp_path, hourly_limit=300, interval=1.0)
    breaker = app.state.p115.breaker
    assert prober.pace() == (1.0, 300)
    breaker.trip("HTTP 405")
    breaker.tripped_at = time.time() - breaker.cooldown - 1
    assert not breaker.tripped  # 冷卻期滿，記下恢復時間
    assert prober.pace() == (4.0, 75) and prober.usage()["slowdown"] == 4
    breaker.recovered_at = time.time() - 2000
    assert prober.pace() == (2.0, 150)
    breaker.recovered_at = time.time() - 4000
    assert prober.pace() == (1.0, 300)
