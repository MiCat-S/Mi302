"""資料庫交易：批次寫入失敗整批不算；讀改寫在同一個交易裡，同時做的兩件事不會互相蓋掉。"""

import sqlite3
import threading
from pathlib import Path

import pytest

from embyserver.auth import AuthService
from embyserver.db import Database


def test_failed_batch_is_rolled_back(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    with pytest.raises(sqlite3.IntegrityError):  # 第二筆和第一筆同一個鍵
        db.executemany("INSERT INTO meta(key, value) VALUES(?, ?)", [("a", "1"), ("a", "2")])
    assert not db.conn.in_transaction
    db.set_meta("b", "1")  # 下一筆無關的寫入不能把失敗那批的第一筆一起 commit
    assert db.get_meta("a") is None and db.get_meta("b") == "1"

    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO meta(key, value) VALUES('c', '1')")
            db.execute("INSERT INTO meta(key, value) VALUES('d', '1')")  # 交易裡的 db.execute 併進同一個交易
            raise RuntimeError
    assert db.get_meta("c") is None and db.get_meta("d") is None
    with db.transaction() as conn:
        with db.transaction():  # 疊起來的併進外面那一個
            conn.execute("INSERT INTO meta(key, value) VALUES('e', '1')")
        assert db.get_meta("e") is None  # 外層還沒結束：還沒 commit
    assert db.get_meta("e") == "1"


def _stall(db: Database, marker: str, parties: int = 2) -> None:
    """讓查詢裡有 marker 的 db.one 停在同一點，等每個執行緒都讀到舊值才放行（舊寫法會兩個都讀到同一個值）。
    改好的寫法在交易裡用 conn 讀、不經過 db.one，等不到其他執行緒就逾時放行。"""
    barrier = threading.Barrier(parties, timeout=1)
    one = db.one

    def stalled(sql, params=()):
        row = one(sql, params)
        if marker in sql:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return row

    db.one = stalled


def _together(*calls) -> list:
    errors: list = []

    def run(call):
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - 收起來給測試看
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_concurrent_changes_do_not_lose_updates(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    auth = AuthService(db, [])
    a = auth.create_user("a", "", True)["id"]
    b = auth.create_user("b", "", True)["id"]

    # 兩個管理員同時刪掉對方（或降級）：只能成功一個，至少留一個管理員
    _stall(db, "is_admin=1")
    errors = _together(lambda: auth.delete_user(a), lambda: auth.delete_user(b))
    assert [str(e) for e in errors] == ["至少要保留一個管理員"]
    assert db.scalar("SELECT COUNT(*) FROM users WHERE is_admin=1") == 1
    c = auth.create_user("c", "", True)["id"]
    left = db.one("SELECT id FROM users WHERE is_admin=1 AND id<>?", (c,))["id"]
    _stall(db, "is_admin=1")
    errors = _together(lambda: auth.update_user(left, admin=False), lambda: auth.update_user(c, admin=False))
    assert len(errors) == 1 and db.scalar("SELECT COUNT(*) FROM users WHERE is_admin=1") == 1

    # 兩個人同時建立 API 金鑰：兩把都要留下
    _stall(db, "FROM meta")
    made: list = []
    assert _together(lambda: made.append(auth.create_api_key("x")), lambda: made.append(auth.create_api_key("y"))) == []
    assert sorted(k["key"] for k in auth.list_api_keys()) == sorted(k["key"] for k in made)
