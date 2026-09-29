"""測試共用的設定。"""

import threading
import time

import pytest

from embyserver.db import Database


@pytest.fixture(autouse=True)
def close_databases(monkeypatch):
    """每個測試結束後關掉它開的資料庫。多數測試直接建 app、不經過 lifespan（程式結束時才關），
    不關的話 Python 3.13 起每個連線都會報 ResourceWarning: unclosed database。
    測試留下的背景工作（例如存設定後的重新掃描）先等它做完，最多幾秒，免得關掉後它才碰資料庫。"""
    before = set(threading.enumerate())
    opened = []
    real_init = Database.__init__

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(Database, "__init__", init)
    yield
    deadline = time.monotonic() + 5
    for t in set(threading.enumerate()) - before:
        t.join(max(0.0, deadline - time.monotonic()))
    for db in opened:
        db.close()
