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
