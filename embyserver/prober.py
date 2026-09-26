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
from collections import deque
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
from .filetypes import LIBRARY_VIDEO_EXTS
from .scanner import read_strm

log = logging.getLogger(__name__)

QUEUE_MAX = 500  # 打開即探測的佇列上限；一次打開很多集時，多的下次再排
RETRY_AFTER = 3600  # 打開即探測失敗的，一小時內不再排
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
        self._fetches: deque = deque()  # 最近一小時向 115 取直鏈的時間（單調時鐘）
        self.waiting_until = 0.0  # 到了每小時上限、要等到的時間（給網頁顯示）
        self.runner: Callable = subprocess.run  # 測試時換掉
        self.clock: Callable[[], float] = time.monotonic
        self.sleep: Callable[[float], None] = time.sleep
        # 打開即探測：一條背景執行緒按序處理，和整庫探測共用間隔、每小時上限和熔斷
        self._queue: deque = deque()
        self._queued: set = set()
        self._failed: dict = {}
        self._qlock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.on_demand_done = 0
        self.on_demand_failed = 0
        self._which: Tuple[float, str, Optional[str]] = (0.0, "", None)  # (查的時間, 設定的路徑, 找到的路徑)

    # ---------------- 狀態 ----------------

    def available(self) -> Optional[str]:
        """ffprobe 的完整路徑；找不到時回傳 None（這時只讀現成的 X-mediainfo.json）。結果快取 60 秒。"""
        want = self.cfg.ffprobe or "ffprobe"
        at, key, found = self._which
        if key != want or time.monotonic() - at > 60:
            found = shutil.which(want)
            self._which = (time.monotonic(), want, found)
        return found

    @property
    def concurrency(self) -> int:
        return max(1, min(int(self.cfg.concurrency or 1), 3))

    def _needs(self, path: Path) -> bool:
        return (
            path.suffix.lower() in LIBRARY_VIDEO_EXTS
            and not sidecar_path(path).exists()
            and self.store.get(str(path)) is None
        )

    def missing(self) -> List[str]:
        """媒體庫裡還沒有媒體資訊的影片（strm 和本機影片），新的先做。"""
        have = {r["path"] for r in self.db.query("SELECT path FROM media_info")}
        found: List[Tuple[float, str]] = []
        exts = LIBRARY_VIDEO_EXTS
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

    def pace(self) -> Tuple[float, int]:
        """目前的取直鏈間隔和每小時上限；熔斷剛恢復時放慢（間隔 ×4、上限 ÷4，之後 ×2、÷2）。"""
        factor = self.p115.breaker.slowdown()
        interval = max(0.5, float(self.cfg.interval or 0)) * factor
        limit = int(self.cfg.hourly_limit or 0)
        return interval, max(1, limit // factor) if limit else 0

    def usage(self) -> dict:
        """這一小時已經向 115 取了幾次直鏈。"""
        now = self.clock()
        interval, limit = self.pace()
        used = sum(1 for t in list(self._fetches) if now - t < 3600)  # 先複製：探測執行緒同時在改這個 deque
        waiting = self.waiting_until if self.waiting_until > time.time() else 0
        return {"used": used, "limit": limit, "interval": interval, "slowdown": self.p115.breaker.slowdown(),
                "waiting_until": int(waiting) or None}

    def _wait_turn(self) -> None:
        """向 115 取直鏈前排隊：全域間隔，加上每小時上限（到了就等最舊的那次滿一小時）。"""
        with self._pace:
            while True:
                interval, limit = self.pace()
                now = self.clock()
                while self._fetches and now - self._fetches[0] >= 3600:
                    self._fetches.popleft()
                if limit and len(self._fetches) >= limit:
                    wait = 3600 - (now - self._fetches[0])
                    self.waiting_until = time.time() + wait
                    log.info("已達每小時 %s 次的上限，%d 分鐘後再向 115 取直鏈", limit, wait // 60 + 1)
                    self.sleep(min(wait, 60))  # 每分鐘醒來看一次，設定改了馬上生效
                    continue
                wait = interval - (now - self._last) if self._last else 0
                if wait > 0:
                    self.sleep(min(wait, interval))
                    continue
                break
            self.waiting_until = 0.0
            self._last = self.clock()
            self._fetches.append(self._last)

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
            except Exception as exc:  # 沒預料到的錯誤只算這一項失敗，不能讓整批探測的執行緒死掉
                with count:
                    r.failed += 1
                    error(f"{path.name}：{type(exc).__name__}: {exc}")
                log.exception("探測 %s 時發生未預期的錯誤", path)
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

    # ---------------- 打開即探測 ----------------

    def enqueue(self, path: str) -> bool:
        """播放器打開了這一項：還沒有媒體資訊就排進背景佇列，立刻返回。"""
        if not self.cfg.on_demand or not self.available():
            return False
        with self._qlock:
            if path in self._queued or len(self._queue) >= QUEUE_MAX:
                return False
            if time.time() - self._failed.get(path, 0) < RETRY_AFTER:
                return False
            if not self._needs(Path(path)):
                return False
            self._queue.append(path)
            self._queued.add(path)
            if self._worker is None:
                self._worker = threading.Thread(target=self._drain, daemon=True)
                self._worker.start()
        return True

    def queue_size(self) -> int:
        return len(self._queue)

    def stop(self) -> None:
        """程式關閉時：不再處理排隊的項目。"""
        self._stop.set()
        with self._qlock:
            self._queue.clear()
            self._queued.clear()

    def _drain(self) -> None:
        try:
            self._drain_queue()
        finally:
            with self._qlock:
                self._worker = None  # 不管怎麼結束，下次 enqueue 都能再開一條

    def _drain_queue(self) -> None:
        while not self._stop.is_set():
            with self._qlock:
                if not self._queue:
                    return
                path = self._queue[0]
            try:
                if self._needs(Path(path)):  # 整庫探測可能已經做過了
                    self.probe_one(Path(path))
                    self.on_demand_done += 1
                    log.info("打開即探測：%s", Path(path).name)
            except (ProbeAbort, P115Throttled) as exc:
                # 後面的也不會成功：清空佇列，一小時後打開再試
                with self._qlock:
                    dropped = list(self._queue)
                    self._queue.clear()
                    self._queued.clear()
                now = time.time()
                for p in dropped:
                    self._failed[p] = now
                self.on_demand_failed += len(dropped)
                log.warning("打開即探測先停下（%s 項）：%s", len(dropped), exc)
                continue
            except ProbeSkip as exc:
                self._failed[path] = time.time()
                log.info("打開即探測略過 %s：%s", Path(path).name, exc)
            except (P115Error, RuntimeError, OSError) as exc:
                self._failed[path] = time.time()
                self.on_demand_failed += 1
                log.warning("打開即探測 %s 失敗：%s", Path(path).name, exc)
            except Exception:  # 沒預料到的錯誤：記下來、跳過這一項，佇列繼續
                self._failed[path] = time.time()
                self.on_demand_failed += 1
                log.exception("打開即探測 %s 時發生未預期的錯誤", Path(path).name)
            with self._qlock:
                if self._queue and self._queue[0] == path:
                    self._queue.popleft()
                self._queued.discard(path)
                if len(self._failed) > 5000:  # 只記最近一小時的
                    cutoff = time.time() - RETRY_AFTER
                    self._failed = {p: t for p, t in self._failed.items() if t >= cutoff}

    def run_in_background(self, paths: Optional[List[str]], source: str) -> bool:
        if self._lock.locked():
            return False
        threading.Thread(target=self.run, args=(paths, source), daemon=True).start()
        return True
