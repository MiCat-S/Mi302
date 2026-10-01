"""Mi302 送進 115 回收站的紀錄：每一條刪除路徑都寫一筆，網頁的「回收站」查得到，要到 115 還原時找得到。

詳細說明見 docs/modules.md 的「embyserver/deletelog.py」。
"""

from __future__ import annotations

import posixpath
import time
from typing import Iterable, List, Tuple

from .db import Database

KEEP = 5000  # 最多留幾筆，舊的刪掉
# 從哪裡刪的（網頁上的名稱）
SOURCES = {"dupes": "重複檔案", "empty": "空資料夾", "browse": "瀏覽 115", "organize": "整理 115 網盤",
           "cleanup": "整理後清舊資料夾"}


def record(db: Database, source: str, rows: Iterable[dict]) -> None:
    """記下這次送進回收站的：rows 每一筆 {file_id, path, name?, is_dir?, size?}；沒給 name 用 path 的最後一段。"""
    now = int(time.time())
    values = [(source, int(r.get("file_id") or 0), str(r.get("name") or posixpath.basename(str(r.get("path") or "").rstrip("/"))),
               str(r.get("path") or ""), int(bool(r.get("is_dir"))), int(r.get("size") or 0), now) for r in rows]
    if not values:
        return
    with db.lock:
        c = db.conn
        c.executemany("INSERT INTO deleted_log(source, file_id, name, path, is_dir, size, at) VALUES(?,?,?,?,?,?,?)", values)
        c.execute("DELETE FROM deleted_log WHERE id <= (SELECT id FROM deleted_log ORDER BY id DESC LIMIT 1 OFFSET ?)", (KEEP,))
        c.commit()


def recent(db: Database, source: str = "", limit: int = 50, offset: int = 0) -> Tuple[List[dict], int]:
    """新的在前面；source 只看一種來源。回傳 (這一頁, 總數)。"""
    where, params = ("WHERE source=?", (source,)) if source else ("", ())
    total = db.one(f"SELECT COUNT(*) AS c FROM deleted_log {where}", params)["c"]
    rows = db.query(f"SELECT source, file_id, name, path, is_dir, size, at FROM deleted_log {where} "
                    "ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset))
    return [{**dict(r), "file_id": str(r["file_id"]), "is_dir": bool(r["is_dir"]), "source_name": SOURCES.get(r["source"], r["source"])}
            for r in rows], total
