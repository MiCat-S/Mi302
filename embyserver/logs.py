"""日誌：同時寫到終端、日誌檔和記憶體。

- 日誌檔：<data_dir>/logs/mi302.log，滿 5 MB 換新檔，保留 5 份舊檔。
- 記憶體：保留最新的 3000 筆，給網頁「日誌」頁即時查看。
- 等級：info（一般）或 debug（詳細，另外記錄每個播放器請求），網頁上可以切換。
"""

from __future__ import annotations

import collections
import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LEVELS = {"debug": logging.DEBUG, "info": logging.INFO}
LOG_FILE = "mi302.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5
# 第三方套件在 debug 等級會非常囉嗦，一律只記警告以上
NOISY = ("httpx", "httpcore", "hpack", "asyncio", "multipart", "python_multipart", "urllib3", "uvicorn.access")
# 網址裡的登入憑證不寫進日誌
SECRET_RE = re.compile(r"(?i)((?:api_?key|x-emby-token|x-mediabrowser-token|token|pw|password)=)[^&\s]*")


def redact(text: str) -> str:
    return SECRET_RE.sub(r"\1***", text)


class Redact(logging.Filter):
    """所有輸出都先遮掉網址裡的 api_key、token 等憑證（包括第三方套件印出的網址）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:
            return True
        clean = redact(text)
        if clean != text:
            record.msg, record.args = clean, None
        return True


REDACT = Redact()


class MemoryLog(logging.Handler):
    """最近的日誌放在記憶體，每筆有遞增的序號，網頁用序號只拿新的。"""

    def __init__(self, capacity: int = 3000):
        super().__init__(logging.DEBUG)
        self.addFilter(REDACT)
        self.records: collections.deque = collections.deque(maxlen=capacity)
        self.seq = 0
        self._fmt = logging.Formatter()
        self._rows_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + self._fmt.formatException(record.exc_info)
            with self._rows_lock:
                self.seq += 1
                self.records.append({
                    "seq": self.seq,
                    "time": record.created,
                    "level": record.levelname,
                    "levelno": record.levelno,
                    "logger": record.name,
                    "message": text,
                })
        except Exception:
            self.handleError(record)

    def query(self, after: int = 0, level: str = "INFO", text: str = "", limit: int = 500) -> dict:
        minimum = logging.getLevelName(level.upper()) if isinstance(level, str) else level
        if not isinstance(minimum, int):
            minimum = logging.INFO
        needle = text.strip().lower()
        with self._rows_lock:
            rows = list(self.records)
            last = self.seq
        out = [
            {k: v for k, v in r.items() if k != "levelno"}
            for r in rows
            if r["seq"] > after and r["levelno"] >= minimum
            and (not needle or needle in r["message"].lower() or needle in r["logger"].lower())
        ]
        return {"items": out[-limit:], "last": last, "truncated": len(out) > limit}

    def dump(self) -> str:
        fmt = logging.Formatter(FORMAT)
        with self._rows_lock:
            rows = list(self.records)
        lines = []
        for r in rows:
            rec = logging.LogRecord(r["logger"], r["levelno"], "", 0, r["message"], None, None)
            rec.created = r["time"]
            rec.msecs = (r["time"] % 1) * 1000
            lines.append(fmt.format(rec))
        return "\n".join(lines) + "\n"


MEMORY = MemoryLog()
_file_path: Optional[Path] = None


def attach() -> None:
    """記憶體日誌掛到 root logger（重複呼叫不會重複掛）。"""
    root = logging.getLogger()
    if MEMORY not in root.handlers:
        root.addHandler(MEMORY)
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)


def setup(data_dir: Path, level: str = "info") -> Optional[Path]:
    """程式啟動時呼叫：終端、日誌檔、記憶體三個輸出。回傳日誌檔路徑（無法寫入時為 None）。"""
    global _file_path
    root = logging.getLogger()
    fmt = logging.Formatter(FORMAT)
    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        root.addHandler(console)
    for h in root.handlers:
        if REDACT not in h.filters:
            h.addFilter(REDACT)
    if _file_path is None:
        path = Path(data_dir).expanduser() / "logs" / LOG_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        except OSError as exc:
            logging.getLogger(__name__).warning("無法寫入日誌檔 %s：%s", path, exc)
        else:
            handler.setFormatter(fmt)
            handler.addFilter(REDACT)
            root.addHandler(handler)
            _file_path = path
    attach()
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    set_level(level)
    return _file_path


def set_level(level: str) -> None:
    logging.getLogger().setLevel(LEVELS.get(str(level).lower(), logging.INFO))


def file_path() -> Optional[Path]:
    return _file_path


def files() -> List[Dict]:
    """目前的日誌檔和換下來的舊檔（新的在前）。"""
    if not _file_path:
        return []
    out = []
    for i in range(BACKUPS + 1):
        p = _file_path if i == 0 else _file_path.with_name(f"{_file_path.name}.{i}")
        if p.is_file():
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "modified": st.st_mtime})
    return out
