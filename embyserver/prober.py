"""用 ffprobe 探測 strm 指向的影片，寫出 X-mediainfo.json 並存進資料庫。

做法參考 xiao-vvv/emby-mediainfo（MIT）：
- 取直鏈和 ffprobe 用同一個 UA（115 的直鏈綁定 UA），而且用一般瀏覽器的 UA：115Browser 的 UA 會被 CDN 要 cookie。
- ffprobe 加 -multiple_requests 1：一個檔案要分段讀幾十次，重用同一條連線，少觸發 115 CDN 限流。
- 115 同時最多 3 條連線；取直鏈有全域間隔（用單調時鐘，系統時間往回跳也不會卡住）；
  被限流就熔斷（P115Service.breaker），剩下的這次先不做。
- 錯誤訊息裡的直鏈網址抹掉，不寫進日誌。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from .config import Config, MediaInfoConfig
from .db import Database
from .mediainfo import MediaInfoStore, build_sidecar, parse_sidecar, sidecar_path, write_sidecar
from .p115 import PLAIN_UA, P115Error, P115Service, P115Throttled, extract_pickcode
from .redirect import apply_path_rules
from .scanner import VIDEO_EXTS, read_strm

log = logging.getLogger(__name__)

FFPROBE_ARGS = ["-threads", "0", "-v", "error", "-print_format", "json", "-show_streams", "-show_chapters", "-show_format"]
MAX_ERRORS = 50


class ProbeSkip(Exception):
    """這一項不用或不能探測（例如 strm 內容不是網址）。"""


class ProbeAbort(Exception):
    """後面的也不會成功（115 限流、沒登入、找不到 ffprobe），整批先停。"""


@dataclass
class ProbeResult:
    source: str = ""  # sync / manual
    started: float = 0.0
    finished: float = 0.0
    running: bool = False
    total: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    current: str = ""
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _clean_error(stderr: bytes, url: str) -> str:
    """ffprobe 的錯誤訊息：抹掉網址，只留最後幾行真正的原因（403、逾時、解碼錯誤）。"""
    text = stderr.decode("utf-8", "replace").replace(url, "<url>")
    text = re.sub(r"https?://\S+", "<url>", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-3:])[-300:] or "沒有錯誤訊息"


class MediaProber:
    def __init__(self, cfg: MediaInfoConfig, config: Config, p115: P115Service, db: Database):
        self.cfg = cfg
        self.config = config
        self.p115 = p115
        self.db = db
        self.store = MediaInfoStore(db)
        self.result = ProbeResult()
        self._lock = threading.Lock()
        self._pace = threading.Lock()
        self._last = 0.0
        self.runner: Callable = subprocess.run  # 測試時換掉

    # ---------------- 狀態 ----------------

    def available(self) -> Optional[str]:
        """ffprobe 的完整路徑；找不到時回傳 None（這時只讀現成的 X-mediainfo.json）。"""
        return shutil.which(self.cfg.ffprobe or "ffprobe")

    @property
    def concurrency(self) -> int:
        return max(1, min(int(self.cfg.concurrency or 1), 3))

    def _needs(self, path: Path) -> bool:
        return (
            path.suffix.lower() in VIDEO_EXTS | {".strm"}
            and not sidecar_path(path).exists()
            and self.store.get(str(path)) is None
        )

    def missing(self) -> List[str]:
        """媒體庫裡還沒有媒體資訊的影片（strm 和本機影片），新的先做。"""
        have = {r["path"] for r in self.db.query("SELECT path FROM media_info")}
        found: List[Tuple[float, str]] = []
        exts = VIDEO_EXTS | {".strm"}
        for lib in self.config.libraries:
            for root in lib.paths:
                for dirpath, _, filenames in os.walk(Path(root).expanduser()):
                    names = set(filenames)
                    for name in filenames:
                        stem, ext = os.path.splitext(name)
                        if ext.lower() not in exts or stem + "-mediainfo.json" in names:
                            continue
                        path = os.path.join(dirpath, name)
                        if path in have:
                            continue
                        try:
                            found.append((os.path.getmtime(path), path))
                        except OSError:
                            continue
        return [p for _, p in sorted(found, reverse=True)]

    # ---------------- 探測一項 ----------------

    def _wait_turn(self) -> None:
        """向 115 取直鏈的全域間隔。"""
        interval = max(0.5, float(self.cfg.interval or 0))
        with self._pace:
            wait = interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(min(wait, interval))
            self._last = time.monotonic()

    def _source(self, path: Path) -> Tuple[str, Optional[str], str]:
        """要給 ffprobe 讀的位置、UA，以及用來判斷容器格式的檔名。"""
        if path.suffix.lower() != ".strm":
            return str(path), None, str(path)
        target = apply_path_rules(read_strm(path), self.config.redirect.path_rules)
        if not target:
            raise ProbeSkip("strm 是空的")
        hint = urlsplit(target).path if target.startswith(("http://", "https://")) else target
        pickcode = extract_pickcode(target)
        if pickcode:
            if not self.p115.logged_in:
                raise ProbeAbort("尚未登入 115，115 的 strm 沒辦法探測")
            self.p115.breaker.check()
            self._wait_turn()
            return self.p115.download_url(pickcode, PLAIN_UA), PLAIN_UA, hint
        if target.startswith(("http://", "https://")):
            return target, PLAIN_UA, hint
        if Path(target).is_file():
            return target, None, target
        raise ProbeSkip("strm 裡不是網址，也不是存在的檔案")

    def probe_one(self, path: Path) -> dict:
        exe = self.available()
        if not exe:
            raise ProbeAbort("找不到 ffprobe，請先安裝 ffmpeg")
        url, ua, hint = self._source(path)
        cmd = [exe]
        if ua:
            cmd += ["-user_agent", ua]
        if url.startswith(("http://", "https://")):
            cmd += ["-multiple_requests", "1"]
        cmd += FFPROBE_ARGS + ["-i", url]
        try:
            proc = self.runner(cmd, capture_output=True, timeout=self.cfg.timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"ffprobe 超過 {self.cfg.timeout} 秒沒有讀完")
        if proc.returncode != 0 or not proc.stdout.strip():
            raise RuntimeError(f"ffprobe 失敗：{_clean_error(proc.stderr or b'', url)}")
        try:
            probe = json.loads(proc.stdout.decode("utf-8", "replace"))
        except ValueError:
            raise RuntimeError("ffprobe 的輸出不是 JSON")
        if not probe.get("streams"):
            raise RuntimeError("ffprobe 沒有讀到任何串流")
        sidecar = build_sidecar(probe, hint)
        info = parse_sidecar(sidecar)
        mtime = 0.0
        try:
            mtime = write_sidecar(path, sidecar).stat().st_mtime
        except OSError as exc:
            # 媒體資料夾唯讀時只存在資料庫
            log.warning("寫不進 %s，媒體資訊只存在資料庫：%s", sidecar_path(path), exc)
        self.store.put(str(path), info, mtime, "ffprobe")
        ticks = info["source"].get("RunTimeTicks")
        if ticks:
            self.db.execute(
                "UPDATE items SET runtime_ticks=? WHERE path=? AND (runtime_ticks IS NULL OR runtime_ticks=0)",
                (int(ticks), str(path)),
            )
        return info

    # ---------------- 整批 ----------------

    def run(self, paths: Optional[Iterable[str]], source: str) -> ProbeResult:
        """paths 為 None 時探測媒體庫裡所有還沒有媒體資訊的影片。"""
        if not self._lock.acquire(blocking=False):
            log.info("媒體資訊探測已在進行，略過")
            return self.result
        r = self.result = ProbeResult(source=source, started=time.time(), running=True)
        abort = threading.Event()
        count = threading.Lock()

        def error(msg: str) -> None:
            if len(r.errors) < MAX_ERRORS:
                r.errors.append(msg)

        def one(path: Path) -> None:
            if abort.is_set():
                return
            r.current = path.name
            try:
                self.probe_one(path)
            except (ProbeAbort, P115Throttled) as exc:
                with count:
                    if not abort.is_set():
                        abort.set()
                        error(str(exc))
                        log.error("媒體資訊探測中止：%s", exc)
                return
            except ProbeSkip as exc:
                with count:
                    r.skipped += 1
                log.info("略過 %s：%s", path.name, exc)
                return
            except (P115Error, RuntimeError, OSError) as exc:
                with count:
                    r.failed += 1
                    error(f"{path.name}：{exc}")
                log.warning("探測 %s 失敗：%s", path, exc)
                return
            with count:
                r.done += 1

        try:
            if not self.available():
                raise ProbeAbort("找不到 ffprobe，請先安裝 ffmpeg")
            todo = self.missing() if paths is None else [p for p in paths if self._needs(Path(p))]
            r.total = len(todo)
            log.info("探測 %s 支影片的媒體資訊（同時 %s 項）", len(todo), self.concurrency)
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                for future in [pool.submit(one, Path(p)) for p in todo]:
                    future.result()
            if abort.is_set():
                r.failed = r.total - r.done - r.skipped  # 中止後沒做的都算沒完成
        except ProbeAbort as exc:
            error(str(exc))
        finally:
            r.running = False
            r.current = ""
            r.finished = time.time()
            self._lock.release()
        log.info("媒體資訊探測完成：成功 %s，失敗 %s，略過 %s", r.done, r.failed, r.skipped)
        return r

    def run_in_background(self, paths: Optional[List[str]], source: str) -> bool:
        if self._lock.locked():
            return False
        threading.Thread(target=self.run, args=(paths, source), daemon=True).start()
        return True
