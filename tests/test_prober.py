"""媒體資訊探測：取直鏈、跑 ffprobe、寫出 X-mediainfo.json；限速、熔斷、各種失敗。"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.mediainfo import sidecar_path
from embyserver.p115 import PLAIN_UA

from test_mediainfo import PROBE


class FakeCDN:
    """網路上的影片：照 Range 回一段；沒指定內容的地方填 0x11（和稀疏檔沒讀到的 0 分得出來）。"""

    def __init__(self, size=1_000_000, head=b"\x1a\x45\xdf\xa3", parts=None, ranged=True, status=None, body=None,
                 content_type="video/x-matroska"):
        self.size, self.parts, self.ranged, self.status, self.body = size, dict(parts or {}), ranged, status, body
        self.parts.setdefault(0, head)
        self.content_type = content_type
        self.requests = []

    def data(self, start, end):
        out = bytearray(b"\x11" * (end - start + 1))
        for offset, blob in self.parts.items():
            lo, hi = max(start, offset), min(end, offset + len(blob) - 1)
            if lo <= hi:
                out[lo - start:hi - start + 1] = blob[lo - offset:hi - offset + 1]
        return bytes(out)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "broken" in request.url.path:
            return httpx.Response(403, text="<html><body>403 Forbidden</body></html>")
        if self.status:
            return httpx.Response(self.status, text=self.body or "")
        if self.body is not None:
            return httpx.Response(200, text=self.body, headers={"Content-Type": "text/html; charset=utf-8"})
        m = re.match(r"bytes=(\d+)-(\d+)", request.headers.get("range", ""))
        if not self.ranged or not m:
            return httpx.Response(200, content=self.data(0, self.size - 1), headers={"Content-Type": self.content_type})
        start, end = int(m.group(1)), min(int(m.group(2)), self.size - 1)
        return httpx.Response(206, content=self.data(start, end), headers={
            "Content-Type": self.content_type, "Content-Range": f"bytes {start}-{end}/{self.size}"})


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
    app.state.p115._fetch_download_url = lambda pc, ua: links.append((pc, ua)) or f"https://cdnfhnfile.115cdn.net/{pc}?t=1"

    def runner(cmd, capture_output, timeout):
        cmds.append(cmd)
        target = cmd[-1]
        if not target.startswith("http"):  # 本機稀疏檔：記下 ffprobe 讀到的內容
            prober.seen = Path(target).read_bytes()
        return subprocess.CompletedProcess(cmd, 0, json.dumps(PROBE).encode(), b"")

    prober.runner = runner
    prober.cdn = FakeCDN()
    prober.http_transport = httpx.MockTransport(lambda r: prober.cdn(r))
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
    # 取直鏈和讀檔用同一個 UA；Mi302 自己讀（帶 cookie），ffprobe 讀本機的稀疏檔
    assert links == [("abcdefghijklmnopq", PLAIN_UA)]
    req = prober.cdn.requests[0]
    assert str(req.url) == "https://cdnfhnfile.115cdn.net/abcdefghijklmnopq?t=1" and req.headers["range"] == "bytes=0-6291455"
    assert req.headers["user-agent"] == PLAIN_UA and req.headers["cookie"] == "UID=1"
    cmd = cmds[0]
    assert "-user_agent" not in cmd and not cmd[-1].startswith("http") and len(prober.seen) == 1_000_000
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
    assert links == []  # 不是 115 的網址不用取直鏈
    assert all("cookie" not in req.headers for req in prober.cdn.requests)  # 不是 115 的網域不帶 cookie
    err = next(e for e in r.errors if e.startswith("Broken"))
    assert "HTTP 403" in err and "403 Forbidden" in err and "cdn.example.com" not in err  # 錯誤訊息抹掉網址


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


def test_cached_link_does_not_use_the_hourly_quota(tmp_path: Path):
    """剛播過（直鏈還在快取裡）的影片：探測直接用快取的直鏈，不排隊、不占每小時名額。"""
    app, prober, links, cmds = make(tmp_path, hourly_limit=1, concurrency=1)
    clock = FakeClock()
    prober.clock, prober.sleep = clock, clock.sleep
    expires = int(time.time()) + 7200  # 115 直鏈的 t 參數是到期時間
    app.state.p115._fetch_download_url = lambda pc, ua: links.append((pc, ua)) or f"https://cdn.115.test/{pc}?t={expires}"
    played, fresh = "abcdefghijklmnop1", "abcdefghijklmnop2"
    strm(tmp_path, "Played", f"http://mi302/d/{played}.mkv")
    strm(tmp_path, "Fresh", f"http://mi302/d/{fresh}.mkv")
    app.state.p115.download_url(played, PLAIN_UA)  # 播放時取過一次，進了快取
    assert prober.run(None, "manual").done == 2
    assert links == [(played, PLAIN_UA), (fresh, PLAIN_UA)]  # 探測只向 115 要了沒快取的那一支
    assert prober.usage()["used"] == 1
    assert sum(clock.slept) < 3600  # 名額只用了一次，不必等到下一個小時


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


def _wait(cond, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_open_enqueues_probe_without_waiting(tmp_path: Path):
    import threading

    app, prober, links, cmds = make(tmp_path, enabled=False)  # 不開批次探測也能打開即探測
    gate = threading.Event()
    real = prober.runner
    prober.runner = lambda cmd, capture_output, timeout: gate.wait(5) and real(cmd, capture_output, timeout)
    movie = strm(tmp_path, "A (2020)", "http://mi302/d/abcdefghijklmnopq.mkv")
    strm(tmp_path, "B (2021)", "http://mi302/d/bbcdefghijklmnopq.mkv")
    app.state.scanner.scan_all()
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    items = c.get("/Items", params={"IncludeItemTypes": "Movie", "Recursive": "true"}, headers=h).json()["Items"]
    assert prober.queue_size() == 0  # 列表不觸發
    mid = next(i["Id"] for i in items if i["Name"] == "A")

    start = time.monotonic()
    pb = c.post(f"/Items/{mid}/PlaybackInfo", headers=h).json()["MediaSources"][0]
    assert time.monotonic() - start < 2 and pb["MediaStreams"] == []  # 不等探測
    c.get(f"/Items/{mid}", headers=h)
    assert prober.queue_size() == 1  # 同一項不重複排
    gate.set()
    assert _wait(lambda: prober.queue_size() == 0 and prober.on_demand_done == 1)
    pb = c.post(f"/Items/{mid}/PlaybackInfo", headers=h).json()["MediaSources"][0]
    assert pb["MediaStreams"][0]["Width"] == 3840 and sidecar_path(movie).exists()
    c.get(f"/Items/{mid}", headers=h)
    assert prober.queue_size() == 0 and len(links) == 1  # 有了就不再探測
    s = c.get("/web/api/mediainfo/status", headers=h).json()["on_demand"]
    assert (s["enabled"], s["done"], s["queue"]) == (True, 1, 0)

    prober.cfg.on_demand = False  # 關掉開關不排
    other = next(i["Id"] for i in items if i["Name"] == "B")
    c.get(f"/Items/{other}", headers=h)
    assert prober.queue_size() == 0


def test_open_while_throttled_drops_queue_for_an_hour(tmp_path: Path):
    app, prober, links, cmds = make(tmp_path)
    movie = strm(tmp_path, "A (2020)", "http://mi302/d/abcdefghijklmnopq.mkv")
    app.state.p115.breaker.trip("HTTP 405")
    assert prober.enqueue(str(movie))
    assert _wait(lambda: prober.queue_size() == 0 and prober.on_demand_failed == 1)
    assert links == [] and not prober.enqueue(str(movie))  # 一小時內不再排
    prober._failed[str(movie)] -= 3601
    app.state.p115.breaker.reset()
    assert prober.enqueue(str(movie))
    assert _wait(lambda: prober.on_demand_done == 1)


def test_usage_while_probing_does_not_crash():
    """網頁查狀態時探測執行緒正在改 deque，不能丟 RuntimeError。"""
    import threading
    from collections import deque

    class Box:
        pass

    from embyserver.prober import MediaProber

    p = MediaProber.__new__(MediaProber)
    p._fetches = deque()
    p.clock = lambda: 0.0
    p.waiting_until = 0.0
    p.pace = lambda: (1.0, 0)
    p.p115 = Box()
    p.p115.breaker = Box()
    p.p115.breaker.slowdown = lambda: 1
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            p._fetches.append(0.0)
            if len(p._fetches) > 1000:
                p._fetches.popleft()

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(2000):
            p.usage()
    finally:
        stop.set()
        t.join()


def _admin(app):
    c = TestClient(app)
    return c, {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}


def test_running_batch_can_grow_shrink_and_stop(tmp_path: Path):
    """提取中改「這次最多幾支」直接套用到這一批；「停止提取」後排隊的不做，沒做的不算失敗。"""
    import threading

    app, prober, links, cmds = make(tmp_path, concurrency=1)
    for i in range(8):
        strm(tmp_path, f"M{i} ({2010 + i})", f"http://mi302:8096/d/{'abcdefghijklmnop' + str(i)}.mkv")
    app.state.scanner.scan_all()
    gate = threading.Event()
    real = prober.runner

    def slow(cmd, capture_output, timeout):
        gate.wait(5)  # 卡住，讓這一批停在「提取中」
        return real(cmd, capture_output=capture_output, timeout=timeout)

    prober.runner = slow
    c, h = _admin(app)
    r = c.post("/web/api/mediainfo/probe", json={"order": "year_asc", "limit": 2}, headers=h).json()
    assert (r["started"], r["count"]) == (True, 2)
    assert _wait(lambda: prober.result.running and prober.result.current)
    r = c.post("/web/api/mediainfo/probe/limit", json={"limit": 5}, headers=h).json()
    assert r["total"] == 5 and r["result"]["label"] == "年份舊的先做・最多 5 支"
    assert c.post("/web/api/mediainfo/probe/limit", json={"limit": 4}, headers=h).json()["total"] == 4
    gate.set()
    assert _wait(lambda: not prober.result.running)
    assert (prober.result.done, prober.result.total, prober.result.stopped) == (4, 4, False)
    assert [pc[-1] for pc, _ in links] == ["0", "1", "2", "3"]  # 照「年份舊的先做」

    gate.clear()
    assert c.post("/web/api/mediainfo/probe", json={"order": "year_asc", "limit": 0}, headers=h).json()["count"] == 4
    assert _wait(lambda: prober.result.running and prober.result.current)
    assert c.post("/web/api/mediainfo/stop", headers=h).json()["stopping"] is True
    gate.set()
    assert _wait(lambda: not prober.result.running)
    res = prober.result
    # 手上那一支：已經在跑 ffprobe 的做完才停；還在等取直鏈的直接放棄。排隊的都不做，也不算失敗
    assert res.stopped and not res.stopping and res.failed == 0 and res.total == res.done <= 1
    assert len(links) <= 5

    # 沒在提取：不能改、不能停
    assert c.post("/web/api/mediainfo/probe/limit", json={"limit": 3}, headers=h).status_code == 409
    assert c.post("/web/api/mediainfo/stop", headers=h).json()["stopping"] is False


def test_hourly_cap_change_or_stop_wakes_a_waiting_batch(tmp_path: Path):
    """到了每小時上限在等的時候：調高上限並儲存，馬上繼續；按停止，馬上結束。"""
    import threading

    app, prober, links, cmds = make(tmp_path, hourly_limit=1, concurrency=1)
    for i in range(3):
        strm(tmp_path, f"W{i}", f"http://mi302:8096/d/{'abcdefghijklmnop' + str(i)}.mkv")
    app.state.scanner.scan_all()
    c, h = _admin(app)

    t = threading.Thread(target=prober.run, args=(None, "manual"))
    t.start()
    assert _wait(lambda: prober.waiting_until > 0)  # 第 2 支在等每小時上限
    assert c.put("/web/api/settings", json={"mediainfo": {"hourly_limit": 2}}, headers=h).status_code == 200
    assert _wait(lambda: prober.result.done == 2)  # 不用等一分鐘
    assert _wait(lambda: prober.waiting_until > 0)  # 第 3 支又碰到新的上限
    assert prober.cancel_batch()
    t.join(5)
    assert not t.is_alive() and prober.result.stopped and prober.result.done == 2 and prober.waiting_until == 0



def _one(tmp_path, cdn, name="A (2020)", ext="mkv"):
    app, prober, links, cmds = make(tmp_path)
    prober.cdn = cdn
    strm(tmp_path, name, f"http://mi302:8096/d/abcdefghijklmnopq.{ext}")
    app.state.scanner.scan_all()
    return app, prober, prober.run(None, "manual"), cmds


def test_mp4_with_moov_at_the_end_is_read_by_boxes(tmp_path: Path):
    """moov 在檔尾的 mp4：照 box 找過去，只讀檔頭和 moov；其他地方在暫存檔裡是 0（沒下載）。"""
    import struct
    size = 9_000_000
    moov = struct.pack(">I4s", 1000, b"moov") + b"m" * 992
    ftyp = struct.pack(">I4s", 24, b"ftyp") + b"isom" + b"\x00" * 12
    mdat = struct.pack(">I4s", size - 24 - 1000, b"mdat")
    cdn = FakeCDN(size=size, head=ftyp + mdat, parts={size - 1000: moov}, content_type="video/mp4")
    app, prober, r, cmds = _one(tmp_path, cdn, ext="mp4")
    assert (r.done, r.failed) == (1, 0), r.errors
    assert [q.headers["range"] for q in cdn.requests] == ["bytes=0-6291455", f"bytes={size - 1000}-{size - 1}"]
    seen = prober.seen
    assert len(seen) == size and seen[size - 1000:] == moov and seen[:32] == ftyp + mdat
    assert seen[7_000_000:7_000_100] == b"\x00" * 100  # 中間沒讀


def test_mkv_reads_head_and_tail(tmp_path: Path):
    size = 20_000_000
    cdn = FakeCDN(size=size, parts={size - 10: b"CUES-TAIL!"})
    app, prober, r, cmds = _one(tmp_path, cdn)
    assert (r.done, r.failed) == (1, 0), r.errors
    tail = size - (2 << 20)
    assert [q.headers["range"] for q in cdn.requests] == ["bytes=0-6291455", f"bytes={tail}-{size - 1}"]
    assert prober.seen[-10:] == b"CUES-TAIL!" and prober.seen[10_000_000:10_000_010] == b"\x00" * 10


def test_error_page_instead_of_video_is_reported(tmp_path: Path):
    cdn = FakeCDN(body="<html><head><title>115</title></head><body>访问过于频繁，请稍后再试</body></html>")
    app, prober, r, cmds = _one(tmp_path, cdn)
    assert r.failed == 1 and cmds == []  # 不跑 ffprobe
    assert "115 回的不是影片（text/html）" in r.errors[0] and "访问过于频繁" in r.errors[0]
    assert "cdn.115.test" not in r.errors[0]


def test_truncated_mp4_without_moov(tmp_path: Path):
    import struct
    size = 9_000_000
    head = struct.pack(">I4s", 24, b"ftyp") + b"isom" + b"\x00" * 12 + struct.pack(">I4s", size + 5000, b"mdat")
    app, prober, r, cmds = _one(tmp_path, FakeCDN(size=size, head=head, content_type="video/mp4"), ext="mp4")
    assert r.failed == 1 and "找不到 moov" in r.errors[0] and "沒有傳完整" in r.errors[0] and cmds == []


def test_server_without_range_falls_back_to_ffprobe_url(tmp_path: Path):
    app, prober, r, cmds = _one(tmp_path, FakeCDN(ranged=False))
    assert r.done == 1
    cmd = cmds[0]
    assert cmd[-1] == "https://cdnfhnfile.115cdn.net/abcdefghijklmnopq?t=1" and "-multiple_requests" in cmd
    assert cmd[cmd.index("-user_agent") + 1] == PLAIN_UA


def test_cdn_throttling_stops_the_batch(tmp_path: Path):
    app, prober, links, cmds = make(tmp_path, concurrency=1)
    prober.cdn = FakeCDN(status=429, body="too many requests")
    for i in range(3):
        strm(tmp_path, f"M{i}", f"http://mi302/d/{'abcdefghijklmnop' + str(i)}.mkv")
    r = prober.run(None, "manual")
    assert r.done == 0 and "限流" in " ".join(r.errors) and len(prober.cdn.requests) == 1  # 第一次就停
    assert app.state.p115.breaker.tripped
