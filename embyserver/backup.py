"""每天自動備份資料庫和設定檔。

資料庫裡有使用者、觀看紀錄、115 登入狀態、同步索引和媒體資訊，壞了只能重來，所以每天備份一份：
- 用另開的唯讀連線做 SQLite 線上備份（WAL 模式下不擋服務，也包含還沒寫回主檔的資料）。
- 設定檔（config.yaml）一起複製一份。
- 放在 <data_dir>/backups，檔名 mi302-年月日-時分秒.db／.yaml，只留最新幾份。

還原：停止 Mi302，把備份的 .db 換成 data/library.db（刪掉 library.db-wal、library.db-shm），再啟動。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^mi302-(\d{8}-\d{6})\.(db|yaml)$")
LAST_META_KEY = "backup_last"
DAY = 86400
DEFAULT_KEEP = 7
REQUIRED_TABLES = {"meta", "users"}


class Backup:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.dir = config.data_path / "backups"
        self._lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def keep(self) -> int:
        return max(0, int(self.config.server.backup_keep or 0))

    def items(self) -> List[dict]:
        """備份檔，新的在前。"""
        out = []
        if self.dir.is_dir():
            for f in self.dir.iterdir():
                m = NAME_RE.match(f.name)
                if m and f.is_file():
                    st = f.stat()
                    out.append({"name": f.name, "kind": m.group(2), "stamp": m.group(1), "size": st.st_size,
                                "time": int(st.st_mtime)})
        return sorted(out, key=lambda x: (x["stamp"], x["kind"]), reverse=True)

    def path_of(self, name: str) -> Optional[Path]:
        """只認這個資料夾裡、名稱格式正確的備份檔，不會被 ../ 帶出去。"""
        if not NAME_RE.match(name or ""):
            return None
        p = self.dir / name
        return p if p.is_file() else None

    def last(self) -> float:
        try:
            return float(self.db.get_meta(LAST_META_KEY) or 0)
        except ValueError:
            return 0.0

    def due(self) -> bool:
        return self.keep > 0 and time.time() - self.last() >= DAY

    def run(self) -> str:
        """立刻備份一份，回傳資料庫備份的檔名。"""
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = self.dir / f"mi302-{stamp}.db"
            tmp = target.with_name(target.name + ".part")
            tmp.unlink(missing_ok=True)
            try:
                self._copy_db(tmp)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            os.replace(tmp, target)
            if self.config.path and Path(self.config.path).is_file():
                shutil.copy2(self.config.path, self.dir / f"mi302-{stamp}.yaml")
            self.db.set_meta(LAST_META_KEY, str(time.time()))
            self._prune()
            log.info("已備份資料庫：%s（%s KB）", target.name, target.stat().st_size // 1024)
            return target.name

    def _copy_db(self, tmp: Path) -> None:
        dst = sqlite3.connect(str(tmp))
        try:
            if self.db.path == ":memory:":
                with self.db.lock:
                    self.db.conn.backup(dst)
            else:
                # 路徑要跳脫成 URI：資料夾名稱有 # 或 ?（例如 NAS 的 Disk#2）時，直接拼字串會開到另一個空資料庫
                src = sqlite3.connect(Path(self.db.path).resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    src.backup(dst)
                finally:
                    src.close()
            tables = {r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            dst.close()
        missing = REQUIRED_TABLES - tables
        if missing:  # 空的備份不能拿去取代舊的好備份
            raise RuntimeError(f"備份出來的資料庫缺少 {'、'.join(sorted(missing))} 表，沒有保存")

    def _prune(self) -> None:
        """只留最新幾次；關閉自動備份時手動備份也留 7 份。"""
        stamps = sorted({i["stamp"] for i in self.items()}, reverse=True)
        for stamp in stamps[self.keep or DEFAULT_KEEP:]:
            for ext in ("db", "yaml"):
                (self.dir / f"mi302-{stamp}.{ext}").unlink(missing_ok=True)

    def start(self) -> None:
        """啟動後先看一次，之後每小時看一次，距上次滿一天就備份。"""

        def loop():
            while True:
                if self.due():
                    try:
                        self.run()
                    except Exception:
                        log.exception("自動備份失敗")
                if self._stop.wait(3600):
                    return

        threading.Thread(target=loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
