"""每天自動備份資料庫和設定檔。"""

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.backup import DAY, LAST_META_KEY
from embyserver.config import load_config


def make(tmp_path: Path, keep: int = 7, data: str = "data"):
    conf = tmp_path / "config.yaml"
    conf.write_text(
        f"server:\n  data_dir: '{tmp_path / data}'\n  backup_keep: {keep}\n"
        "users:\n  - name: admin\n    password: pw\n    admin: true\n  - name: kid\n    password: pw\n",
        encoding="utf-8",
    )
    app = create_app(load_config(str(conf)), scan_on_start=False)
    return app, app.state.backup


def test_backup_copies_database_and_config(tmp_path: Path):
    app, bk = make(tmp_path)
    assert bk.due()  # 還沒備份過
    name = bk.run()
    db = sqlite3.connect(bk.dir / name)
    assert {r[0] for r in db.execute("SELECT name FROM users")} == {"admin", "kid"}
    db.close()
    stamp = name[len("mi302-"):-len(".db")]
    assert (bk.dir / f"mi302-{stamp}.yaml").read_text(encoding="utf-8").startswith("server:")
    assert not bk.due()  # 一天內不再自動備份
    app.state.db.set_meta(LAST_META_KEY, str(time.time() - DAY - 1))
    assert bk.due()


def test_keeps_only_the_newest(tmp_path: Path):
    app, bk = make(tmp_path, keep=2)
    bk.dir.mkdir(parents=True)
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        (bk.dir / f"mi302-{stamp}.db").write_text("x")
        (bk.dir / f"mi302-{stamp}.yaml").write_text("x")
    (bk.dir / "notes.txt").write_text("別刪我")
    newest = bk.run()
    assert [i["name"] for i in bk.items() if i["kind"] == "db"] == [newest, "mi302-20260103-000000.db"]
    assert (bk.dir / "notes.txt").exists()

    app.state.config.server.backup_keep = 0  # 關掉自動備份
    assert not bk.due()
    for i in range(8):
        (bk.dir / f"mi302-2025010{i}-000000.db").write_text("x")
    bk.run()
    assert len([i for i in bk.items() if i["kind"] == "db"]) == 7  # 手動備份仍留 7 份


def test_backup_endpoints(tmp_path: Path):
    app, bk = make(tmp_path)
    c = TestClient(app)
    login = lambda u: {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": u, "Pw": "pw"}).json()["AccessToken"]}
    admin, kid = login("admin"), login("kid")
    assert c.get("/web/api/backups", headers=kid).status_code == 403
    name = c.post("/web/api/backups", headers=admin).json()["name"]
    listing = c.get("/web/api/backups", headers=admin).json()
    assert listing["keep"] == 7 and listing["items"][0]["name"] in (name, name.replace(".db", ".yaml"))
    r = c.get(f"/web/api/backups/{name}", headers=admin)
    assert r.status_code == 200 and r.content[:16] == b"SQLite format 3\x00"
    assert c.get(f"/web/api/backups/{name}", headers=kid).status_code == 403
    assert c.get("/web/api/backups/..%2Fconfig.yaml", headers=admin).status_code == 404
    assert c.get("/web/api/backups/mi302-20990101-000000.db", headers=admin).status_code == 404


def test_data_dir_with_hash_or_question_mark(tmp_path: Path):
    app, bk = make(tmp_path, data="Disk#2/mi?302/data")
    db = sqlite3.connect(bk.dir / bk.run())
    assert {r[0] for r in db.execute("SELECT name FROM users")} == {"admin", "kid"}
    db.close()
    assert sorted(f.name for f in tmp_path.iterdir()) == ["Disk#2", "config.yaml"]  # 沒有在旁邊開出空資料庫


def test_empty_backup_is_not_kept(tmp_path: Path):
    app, bk = make(tmp_path)
    good = bk.run()
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    app.state.db.path = str(empty)  # 模擬開到另一個空資料庫
    time.sleep(1.1)  # 檔名精確到秒
    with pytest.raises(RuntimeError, match="缺少"):
        bk.run()
    assert [i["name"] for i in bk.items() if i["kind"] == "db"] == [good]
    assert not list(bk.dir.glob("*.part"))
