"""按了「停止」：都在安全的地方停下。同步不刪沒列到的 strm、掃描不刪沒掃到的項目、刪重複做完這一批就不再刪；
下一次照常做（停止只算那一次）。"""

from pathlib import Path

from embyserver.strm_sync import FULL

from fakes import T0, Fake115, build_dupes, make, make_config, touch, wait_dupes


def test_stopped_sync_keeps_strm_and_next_sync_runs(tmp_path: Path):
    fake = Fake115()
    fake.files += [{"fid": 3 + i, "cid": 103, "n": f"Dark.S01E0{2 + i}.mkv", "pc": f"{i}" * 17, "s": 1, "te": T0} for i in range(3)]
    sync = make(tmp_path, fake, delete_stale=True)
    sync.run(FULL)
    movie = tmp_path / "media" / "電影" / "Old Movie (2001).strm"
    fake.files = [f for f in fake.files if f["fid"] != 1]  # 115 上刪掉電影
    real = sync._handle_file

    def stop_after_first(*a):
        sync.workers.cancel.set()  # 同步到一半按了停止
        return real(*a)

    sync._handle_file = stop_after_first
    r = sync.run(FULL)
    assert r.stopped and r.removed == 0 and movie.exists()  # 沒列完不能當成已經刪掉
    assert any("按了停止" in n for n in r.notes)
    sync._handle_file = real
    r = sync.run(FULL)  # 停止只算那一次
    assert not r.stopped and not movie.exists()
    # 結果摘要記在資料庫：重新啟動後網頁照樣顯示上次同步（不是「還沒同步過」）
    from embyserver.strm_sync import StrmSync
    again = StrmSync(sync.p115, sync.cfg).result
    assert (again.started, again.finished, again.running, again.removed) == (r.started, r.finished, False, 1)


def test_stopped_scan_keeps_items(tmp_path: Path):
    from embyserver.app import create_app

    for name in ("A (2001)", "B (2002)"):
        touch(tmp_path / "movies" / name / f"{name}.strm", "http://x/a.mkv")
    app = create_app(make_config(tmp_path), scan_on_start=False)
    sc, db = app.state.scanner, app.state.db
    sc.scan_all()
    (tmp_path / "movies" / "B (2002)" / "B (2002).strm").unlink()
    real = sc._scan_library

    def stop_first(lib):
        sc.workers.cancel.set()
        return real(lib)

    sc._scan_library = stop_first
    sc.scan_all()
    assert sc.stopped and db.one("SELECT COUNT(*) AS c FROM items WHERE type='Movie'")["c"] == 2  # 沒掃完不刪
    sc._scan_library = real
    sc.scan_all()
    assert not sc.stopped and db.one("SELECT COUNT(*) AS c FROM items WHERE type='Movie'")["c"] == 1
    assert (sc.last["what"], sc.last["touched"], sc.last["stopped"]) == ("全部媒體庫", 1, False)  # 網頁顯示上次掃描

    # 停止是停這一串：正在掃的那一次停下，排在後面等著的（開機、同步後、設定改了）也不做
    import threading

    class WatchedLock:  # 第二個來等鎖的時候通知測試（它排隊的時間已經記下了）
        def __init__(self):
            self.lock, self.waiting = threading.Lock(), threading.Event()

        def __enter__(self):
            if self.lock.locked():
                self.waiting.set()
            self.lock.acquire()

        def __exit__(self, *exc):
            self.lock.release()

    gate, started = threading.Event(), threading.Event()
    sc._lock = WatchedLock()
    sc._scan_library = lambda lib: (started.set(), gate.wait(5), real(lib))[-1]
    first = threading.Thread(target=sc.scan_all)
    first.start()
    assert started.wait(5)
    queued = threading.Thread(target=sc.scan_libraries, args=({"電影"},))
    queued.start()
    assert sc._lock.waiting.wait(5) and sc.cancel()
    gate.set()
    first.join(5)
    queued.join(5)
    assert sc.stopped and (sc.last["what"], sc.last["stopped"]) == ("全部媒體庫", True)  # 排隊的那次沒掃
    sc._scan_library = real
    sc.scan_all()  # 停止之後新開始的照常掃
    assert not sc.stopped


def test_stopped_delete_finishes_the_batch_and_stops(tmp_path: Path, monkeypatch):
    import embyserver.dupes as dupes

    app, fake, media, c, h = build_dupes(tmp_path)
    c.post("/web/api/dupes/scan", json={"paths": ["/"]}, headers=h)
    assert not wait_dupes(app).errors
    monkeypatch.setattr(dupes, "DELETE_BATCH", 1)
    finder = app.state.dupes
    real = finder.p115.delete_files

    def stop_after_first(ids):
        finder.workers.cancel.set()  # 送出第一批時按了停止
        return real(ids)

    finder.p115.delete_files = stop_after_first
    before = len(fake.deleted)  # 同步時刪過匯出的目錄樹
    plan = finder.plan({}, use_suggestions=True)
    assert len(plan) == 2
    assert finder.delete_in_background(plan)
    job = wait_dupes(app)
    assert job.stopped and job.done == 1 and len(fake.deleted) - before == 1
    assert "按了停止，還有 1 個沒刪" in job.errors[0]
