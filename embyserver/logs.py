"""日誌：同時寫到終端、日誌檔和記憶體。

- 日誌檔：<data_dir>/logs/mi302.log，滿 5 MB 換新檔，保留 5 份舊檔。
- 記憶體：保留最新的 3000 筆，給網頁「日誌」頁即時查看。
- 等級：info（一般）或 debug（詳細，另外記錄每個播放器請求），網頁上可以切換。
- 單獨執行時掛在 root logger；在 MoviePilot 裡當插件時只掛在 Mi302 自己的 logger（不動別人的日誌），見 setup(embedded=True)。
"""

from __future__ import annotations

import collections
import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Sequence

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
PACKAGE = __name__.rpartition(".")[0]  # embyserver；在 MoviePilot 裡是 app.plugins.mi302.embyserver
ACCESS = PACKAGE + ".access"  # 詳細模式記錄每個播放器請求
LEVELS = {"debug": logging.DEBUG, "info": logging.INFO}
LOG_FILE = "mi302.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5
# 第三方套件在 debug 等級會非常囉嗦，一律只記警告以上
NOISY = ("httpx", "httpcore", "hpack", "asyncio", "multipart", "python_multipart", "urllib3", "uvicorn.access")
# 網址裡的登入憑證不寫進日誌（refresh_ui 是阿里雲盤線上換 token 時帶的 refresh token）
SECRET_RE = re.compile(r"(?i)((?:api_?key|x-emby-token|x-mediabrowser-token|token|pw|password|refresh_ui)=)[^&\s]*")


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
_target = logging.getLogger()  # 日誌掛在哪個 logger：單獨執行是 root，插件模式是 PACKAGE
_embedded = False
OWNED = "_mi302_log"  # 插件模式加的 handler 帶這個標記：插件重新載入後是新的模組，舊的 handler 靠它認出來拿掉


def attach() -> None:
    """記憶體日誌掛上去（重複呼叫不會重複掛）。"""
    if MEMORY not in _target.handlers:
        _target.addHandler(MEMORY)
    if _embedded:
        return  # 第三方套件的 logger 是整個程序共用的，插件不改
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)


def setup(data_dir: Path, level: str = "info", embedded: bool = False, extra: Sequence[logging.Handler] = ()) -> Optional[Path]:
    """程式啟動時呼叫：終端、日誌檔、記憶體三個輸出。回傳日誌檔路徑（無法寫入時為 None）。

    embedded：在 MoviePilot 裡當插件。只掛在 Mi302 自己的 logger、不往上傳，不加終端輸出（extra 是插件給的，
    例如轉送到 MoviePilot 的插件日誌）；停下時呼叫 detach() 拿掉。"""
    global _file_path, _target, _embedded
    fmt = logging.Formatter(FORMAT)
    if embedded:
        _target, _embedded = logging.getLogger(PACKAGE), True
        _target.propagate = False  # 不往上傳到宿主的 root（detach 時改回來）
        for h in list(_target.handlers):  # 上次載入的模組留下的
            if getattr(h, OWNED, False):
                _target.removeHandler(h)
                if h is not MEMORY:
                    h.close()
        for h in extra:
            setattr(h, OWNED, True)
            h.addFilter(REDACT)
            _target.addHandler(h)
    else:
        root = _target
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
            setattr(handler, OWNED, embedded)
            _target.addHandler(handler)
            _file_path = path
    if embedded:
        setattr(MEMORY, OWNED, True)
    attach()  # 順便把 httpx 等吵的 logger 壓到 WARNING（插件模式不壓）
    set_level(level)
    return _file_path


def detach() -> None:
    """插件停下時：拿掉 setup(embedded=True) 加的 handler、關掉日誌檔，回到單獨執行的預設。"""
    global _file_path, _target, _embedded
    if not _embedded:
        return
    for h in list(_target.handlers):
        if getattr(h, OWNED, False):
            _target.removeHandler(h)
            if h is not MEMORY:  # 記憶體日誌留著：重新啟動後網頁上還看得到之前的
                h.close()
    _target.propagate = True
    _target, _embedded, _file_path = logging.getLogger(), False, None


def set_level(level: str) -> None:
    _target.setLevel(LEVELS.get(str(level).lower(), logging.INFO))


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
