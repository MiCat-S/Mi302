"""用 ffprobe 探測 strm 指向的影片，寫出 X-mediainfo.json 並存進資料庫。

做法參考 xiao-vvv/emby-mediainfo（MIT）：
- 取直鏈用一般瀏覽器的 UA（115 的直鏈通常綁定取得時的 UA；115Browser 的 UA 會被 CDN 要 cookie）。
- 網路上的影片不讓 ffprobe 自己去讀：它會開好幾條連線來回跳著讀（mp4 的 moov 常在檔尾），115 的 CDN
  常常拒絕，結果是「moov atom not found」「Invalid data found」。改成 Mi302 用一條連線、一段一段讀
  需要的部分（檔頭幾 MB；mp4 照 box 找到 moov；其他格式讀檔尾一段），帶著取直鏈的 UA 和 cookie，
  寫進和原檔一樣大的稀疏暫存檔（沒讀的地方不佔空間），ffprobe 讀這個本機檔。
  115 回的不是影片（錯誤網頁、空的）時，錯誤訊息直接寫出 115 回了什麼。
- 伺服器不支援分段讀取（Range）時，才照舊讓 ffprobe 直接讀網址。
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
import struct
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

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
# 網路上的影片由 Mi302 讀這幾段給 ffprobe（見模組說明）
HEAD_BYTES = 6 << 20  # 檔頭；ffprobe 預設最多分析 5 MB
TAIL_BYTES = 2 << 20  # 檔尾（mkv 的 Cues、Tags，ts 的最後時間戳，avi 的索引）
MOOV_MAX = 64 << 20  # mp4 的 moov 超過這麼大就不讀
MP4_BOXES = {b"ftyp", b"styp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot", b"uuid", b"sidx", b"moof"}


class ProbeSkip(Exception):
    """這一項不用或不能探測（例如 strm 內容不是網址）。"""


class ProbeAbort(Exception):
    """後面的也不會成功（115 限流、沒登入、找不到 ffprobe），整批先停。"""


class ProbeCancelled(Exception):
    """使用者按了「停止提取」：還在排隊等取直鏈的那一支不做了。"""


@dataclass
class ProbeResult:
    source: str = ""  # sync / manual
    label: str = ""  # 這一批挑了哪些，例如「劇集・2019–2023・最近加入的先做」「「庆余年」」
    limit: int = 0  # 這一批最多幾支（0 = 全部符合的）；提取中可以改
    stopping: bool = False  # 按了停止，等手上這幾支做完
    stopped: bool = False  # 是按停止結束的（沒做的不算失敗）
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


def _clean_error(stderr: bytes, url: str, shown: str = "") -> str:
    """ffprobe 的錯誤訊息：抹掉網址（或暫存檔路徑），只留最後幾行真正的原因（403、逾時、解碼錯誤）。"""
    text = stderr.decode("utf-8", "replace").replace(url, shown or "<url>")
    text = re.sub(r"https?://\S+", "<url>", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-3:])[-300:] or "沒有錯誤訊息"


class _Remote:
    """用一條連線、一段一段讀網路上的檔案（HTTP Range）。記下檔案大小、類型、伺服器有沒有照 Range 回。"""

    def __init__(self, client: httpx.Client, url: str, headers: dict, p115: P115Service):
        self.client, self.url, self.headers, self.p115 = client, url, headers, p115
        self.total: Optional[int] = None
        self.ranged = True
        self.content_type = ""

    def read(self, start: int, end: int) -> bytes:
        """讀 [start, end]（含兩端）。伺服器不照 Range 回、又不是從頭讀時回傳空的。"""
        want = end - start + 1
        try:
            with self.client.stream("GET", self.url, headers={**self.headers, "Range": f"bytes={start}-{end}"}) as resp:
                if resp.status_code in (405, 429):
                    self.p115.breaker.inspect(status=resp.status_code)
                    raise P115Throttled(self.p115.breaker.message())
                if resp.status_code not in (200, 206):
                    body = resp.read()[:400]
                    raise RuntimeError(f"115 回 HTTP {resp.status_code}{_snippet(body)}")
                self.content_type = resp.headers.get("content-type", "")
                if resp.status_code == 206:
                    m = re.search(r"/(\d+)\s*$", resp.headers.get("content-range", ""))
                    self.total = int(m.group(1)) if m else self.total
                else:
                    self.ranged = False
                    length = resp.headers.get("content-length")
                    self.total = int(length) if length and length.isdigit() else self.total
                    if start > 0:
                        return b""
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if len(buf) >= want:
                        break
                return bytes(buf[:want])
        except httpx.HTTPError as exc:
            raise RuntimeError(f"讀 115 上的檔案失敗：{type(exc).__name__}") from None


def _snippet(body: bytes) -> str:
    """115 回的內容（錯誤網頁、JSON）挑前面一段文字，方便看出原因。"""
    text = re.sub(r"<[^>]+>", " ", body.decode("utf-8", "replace"))
    text = re.sub(r"\s+", " ", text).strip()
    return f"：{text[:120]}" if text else ""


def _not_media(head: bytes, content_type: str) -> Optional[str]:
    """115 回的不是影片（錯誤網頁、JSON、空的）時回傳說明。"""
    if not head:
        return "115 回了空的內容，不是影片"
    kind = content_type.split(";")[0].strip().lower()
    first = head[:64].lstrip()[:1]
    if kind.startswith("text/") or kind in ("application/json", "application/xml") or first in (b"<", b"{"):
        return f"115 回的不是影片（{kind or '沒有類型'}）{_snippet(head[:400])}"
    return None


def _mp4_moov(remote: "_Remote", head: bytes, total: int) -> List[Tuple[int, bytes]]:
    """mp4 照頂層 box 一個一個往後找 moov（影片索引）；整個在檔頭裡就不用另外讀。找不到就是檔案沒傳完整。"""
    offset = 0
    for _ in range(32):
        if offset >= total:
            break
        if offset + 16 <= len(head):
            size, box = _box(head, offset, total - offset)
        elif total - offset <= MOOV_MAX:
            # 剩下的不大（通常就是檔尾的 moov）：一次讀完，在裡面找
            rest = remote.read(offset, total - 1)
            if _has_box(rest, b"moov"):
                return [(offset, rest)]
            break
        else:
            size, box = _box(remote.read(offset, offset + 15), 0, total - offset)
        if size < 8:
            break
        if box == b"moov":
            if offset + size <= len(head):
                return []
            if size > MOOV_MAX:
                raise RuntimeError(f"mp4 的 moov 有 {size >> 20} MB，太大了不讀")
            return [(offset, remote.read(offset, offset + size - 1))]
        offset += size
    raise RuntimeError("mp4 檔裡找不到 moov（影片索引），檔案可能沒有傳完整，播放器多半也放不了")


def _box(data: bytes, offset: int, left: int) -> Tuple[int, bytes]:
    """頂層 box 的大小和類型；大小 0 表示到檔尾，1 表示後面接 64 位元的大小。"""
    if len(data) < offset + 8:
        return 0, b""
    size, box = struct.unpack(">I4s", data[offset:offset + 8])
    if size == 1:
        size = struct.unpack(">Q", data[offset + 8:offset + 16])[0] if len(data) >= offset + 16 else 0
    elif size == 0:
        size = left
    return size, box


def _has_box(data: bytes, want: bytes) -> bool:
    """data 從頭開始是一串頂層 box，裡面有沒有 want。"""
    offset = 0
    while offset + 8 <= len(data):
        size, box = _box(data, offset, len(data) - offset)
        if box == want:
            return True
        if size < 8:
            return False
        offset += size
    return False


def _add_error(r: ProbeResult, msg: str) -> None:
    """錯誤訊息最多留 MAX_ERRORS 則，一整批都失敗時網頁不會被塞爆。"""
    if len(r.errors) < MAX_ERRORS:
        r.errors.append(msg)


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
        self.http_transport: Optional[httpx.BaseTransport] = None  # 讀網路上的影片用；測試時換成假的
        self.clock: Callable[[], float] = time.monotonic
        self._wake = threading.Event()
        self.sleep: Callable[[float], None] = self._nap  # 測試時換成不真的等的假時鐘
        # 手動提取的這一批：排隊中的影片、已經拿去做的影片；提取中可以加減、停止
        self._pending: deque = deque()
        self._taken: set = set()
        self._batch_lock = threading.Lock()
        self._cancel = threading.Event()
        self.batch_spec: Optional[dict] = None  # 網頁挑這一批時的條件，改「這次最多幾支」時用來補
        # 打開即探測：一條背景執行緒按序處理，和批次探測共用間隔、每小時上限和熔斷
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

    def _nap(self, seconds: float) -> None:
        """等一下；設定改了（wake）或按了停止時提早醒來。"""
        self._wake.wait(seconds)
        self._wake.clear()

    def wake(self) -> None:
        """設定改了：在等取直鏈間隔或每小時上限的，馬上照新設定重新算。"""
        self._wake.set()

    def _wait_turn(self, cancel: Optional[threading.Event] = None) -> None:
        """向 115 取直鏈前排隊：全域間隔，加上每小時上限（到了就等最舊的那次滿一小時）。

        等的時候每分鐘醒來看一次；設定改了（wake）馬上重算，cancel 被設了就放棄（ProbeCancelled）。
        """
        with self._pace:
            while True:
                if cancel is not None and cancel.is_set():
                    self.waiting_until = 0.0
                    raise ProbeCancelled()
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

    def _source(self, path: Path, cancel: Optional[threading.Event] = None) -> Tuple[str, Optional[str], str]:
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
            cached = self.p115.cached_download_url(pickcode, PLAIN_UA)
            if cached:
                return cached, PLAIN_UA, hint  # 剛播過、直鏈還在快取裡：不向 115 要，不占間隔和每小時名額
            self._wait_turn(cancel)
            return self.p115.download_url(pickcode, PLAIN_UA), PLAIN_UA, hint
        if target.startswith(("http://", "https://")):
            return target, PLAIN_UA, hint
        if Path(target).is_file():
            return target, None, target
        raise ProbeSkip("strm 裡不是網址，也不是存在的檔案")

    def _ffprobe(self, exe: str, target: str, ua: Optional[str], shown: str = "") -> dict:
        """跑 ffprobe，回傳它的 JSON；target 是網址或本機檔。錯誤訊息裡的 target 換成 shown（抹掉網址）。"""
        cmd = [exe]
        if ua:
            cmd += ["-user_agent", ua]
        if target.startswith(("http://", "https://")):
            cmd += ["-multiple_requests", "1"]
        cmd += FFPROBE_ARGS + ["-i", target]
        try:
            proc = self.runner(cmd, capture_output=True, timeout=self.cfg.timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"ffprobe 超過 {self.cfg.timeout} 秒沒有讀完")
        if proc.returncode != 0 or not proc.stdout.strip():
            raise RuntimeError(f"ffprobe 失敗：{_clean_error(proc.stderr or b'', target, shown)}")
        try:
            probe = json.loads(proc.stdout.decode("utf-8", "replace"))
        except ValueError:
            raise RuntimeError("ffprobe 的輸出不是 JSON")
        if not probe.get("streams"):
            raise RuntimeError("ffprobe 沒有讀到任何串流")
        return probe

    def _probe_remote(self, exe: str, url: str, ua: str, hint: str) -> dict:
        """網路上的影片：Mi302 用一條連線讀需要的幾段，寫進稀疏暫存檔給 ffprobe 讀（見模組說明）。"""
        headers = self.p115.file_headers(url, ua)  # 取直鏈的 UA；115 的網域再帶 cookie
        timeout = httpx.Timeout(max(10.0, float(self.cfg.timeout or 60)), connect=15.0)
        with httpx.Client(timeout=timeout, follow_redirects=True, transport=self.http_transport) as client:
            remote = _Remote(client, url, headers, self.p115)
            head = remote.read(0, HEAD_BYTES - 1)
            problem = _not_media(head, remote.content_type)
            if problem:
                raise RuntimeError(problem)
            total = remote.total
            if not remote.ranged or not total:
                # 不支援分段讀取：只能讓 ffprobe 自己讀網址
                return self._ffprobe(exe, url, ua)
            pieces = [(0, head)]
            if total > len(head):
                if head[4:8] in MP4_BOXES:
                    pieces += _mp4_moov(remote, head, total)
                else:
                    start = max(len(head), total - TAIL_BYTES)
                    pieces.append((start, remote.read(start, total - 1)))
        name = Path(hint).name or "video"
        with tempfile.TemporaryDirectory(prefix="mi302-probe-") as tmp:
            local = Path(tmp) / ("probe" + Path(name).suffix.lower())
            with open(local, "wb") as f:
                f.truncate(total)  # 稀疏檔：沒寫的地方不佔空間
                for offset, data in pieces:
                    f.seek(offset)
                    f.write(data)
            try:
                return self._ffprobe(exe, str(local), None, shown=name)
            except RuntimeError as exc:
                raise RuntimeError(f"{exc}（檔頭 {head[:8].hex()}，共 {total} 位元組）") from None

    def probe_one(self, path: Path, cancel: Optional[threading.Event] = None) -> dict:
        exe = self.available()
        if not exe:
            raise ProbeAbort("找不到 ffprobe，請先安裝 ffmpeg")
        url, ua, hint = self._source(path, cancel)
        if url.startswith(("http://", "https://")):
            probe = self._probe_remote(exe, url, ua or PLAIN_UA, hint)
        else:
            probe = self._ffprobe(exe, url, None)
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

    def run(self, paths: Optional[Iterable[str]], source: str, label: str = "", limit: int = 0) -> ProbeResult:
        """依序探測 paths（網頁挑好、排好的順序）；paths 為 None 時探測媒體庫裡所有還沒有媒體資訊的影片。

        幾條工作執行緒輪流從排隊的清單拿下一支，所以提取中可以加減（retarget）或停止（cancel_batch）。
        """
        if not self._lock.acquire(blocking=False):
            log.info("媒體資訊探測已在進行，略過")
            return self.result
        r = self.result = ProbeResult(source=source, label=label, limit=limit, started=time.time(), running=True)
        self._cancel.clear()
        abort = threading.Event()
        count = threading.Lock()

        def worker() -> None:
            while not abort.is_set() and not self._cancel.is_set():
                with self._batch_lock:
                    if not self._pending:
                        return
                    path = self._pending.popleft()
                    self._taken.add(path)
                self._batch_one(r, Path(path), abort, count)

        try:
            if not self.available():
                raise ProbeAbort("找不到 ffprobe，請先安裝 ffmpeg")
            todo = self.missing() if paths is None else [p for p in paths if self._needs(Path(p))]
            with self._batch_lock:
                self._pending, self._taken = deque(todo), set()
                r.total = len(todo)
            log.info("探測 %s 支影片的媒體資訊（同時 %s 項）", len(todo), self.concurrency)
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                for future in [pool.submit(worker) for _ in range(self.concurrency)]:
                    future.result()
            if self._cancel.is_set():
                r.stopped = True
                r.total = r.done + r.failed + r.skipped  # 沒做的不算
            elif abort.is_set():
                r.failed = r.total - r.done - r.skipped  # 中止後沒做的都算沒完成
        except ProbeAbort as exc:
            _add_error(r, str(exc))
        finally:
            with self._batch_lock:
                self._pending.clear()
            r.running = r.stopping = False
            r.current = ""
            r.finished = time.time()
            self._lock.release()
        log.info("媒體資訊探測%s：成功 %s，失敗 %s，略過 %s", "停止" if r.stopped else "完成", r.done, r.failed, r.skipped)
        return r

    def _batch_one(self, r: ProbeResult, path: Path, abort: threading.Event, count: threading.Lock) -> None:
        """整批裡的一支：成功、略過、失敗各記一筆。整批都做不下去的（115 限流、找不到 ffprobe）設 abort，
        其他工作執行緒看到就停手，錯誤只記一次。"""
        if abort.is_set():
            return
        r.current = path.name
        try:
            if not self._needs(path):
                raise ProbeSkip("已經有媒體資訊")  # 排隊的時候被打開即探測做掉、或旁邊放了 json
            self.probe_one(path, self._cancel)
        except ProbeCancelled:
            return  # 按了停止：這一支沒做，不算
        except (ProbeAbort, P115Throttled) as exc:
            with count:
                if not abort.is_set():
                    abort.set()
                    _add_error(r, str(exc))
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
                _add_error(r, f"{path.name}：{exc}")
            log.warning("探測 %s 失敗：%s", path, exc)
            return
        except Exception as exc:  # 沒預料到的錯誤只算這一項失敗，不能讓整批探測的執行緒死掉
            with count:
                r.failed += 1
                _add_error(r, f"{path.name}：{type(exc).__name__}: {exc}")
            log.exception("探測 %s 時發生未預期的錯誤", path)
            return
        with count:
            r.done += 1

    def retarget(self, candidates: List[str], limit: int, label: str = "") -> Optional[int]:
        """正在跑的這一批改成最多 limit 支（0 = candidates 全部）：多了從 candidates 依序補上，少了把排隊的從後面拿掉。

        candidates 是照原本的條件、順序重新挑的清單；已經做過或排隊中的不會重複。回傳這一批現在共幾支；沒在跑時回傳 None。
        """
        r = self.result
        if not (self._lock.locked() and r.running) or self._cancel.is_set():
            return None
        with self._batch_lock:
            started = r.total - len(self._pending)  # 已經拿去做的（做完或手上正在做）
            known = self._taken | set(self._pending)
            fresh = [p for p in candidates if p not in known]
            want = limit if limit > 0 else started + len(self._pending) + len(fresh)
            room = want - started - len(self._pending)
            if room > 0:
                self._pending.extend(fresh[:room])
            else:
                for _ in range(min(-room, len(self._pending))):
                    self._pending.pop()
            r.total = started + len(self._pending)
            r.limit = limit
            if label:
                r.label = label
        log.info("這一批媒體資訊提取改成最多 %s 支，共 %s 支", limit or "全部", r.total)
        return r.total

    def cancel_batch(self) -> bool:
        """停止正在跑的這一批：排隊的不做了，手上正在做的做完就停。"""
        if not (self._lock.locked() and self.result.running):
            return False
        self._cancel.set()
        with self._batch_lock:
            self._pending.clear()
        self.result.stopping = True
        self.wake()  # 在等每小時上限或間隔的，馬上醒來
        log.info("停止媒體資訊提取，等手上的做完")
        return True

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
        """程式關閉時：不再處理排隊的項目，正在跑的這一批也停下。"""
        self._stop.set()
        with self._qlock:
            self._queue.clear()
            self._queued.clear()
        self.cancel_batch()
        self.wake()

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
                if self._needs(Path(path)):  # 批次探測可能已經做過了
                    self.probe_one(Path(path), self._stop)
                    self.on_demand_done += 1
                    log.info("打開即探測：%s", Path(path).name)
            except ProbeCancelled:
                return  # 程式要關了
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

    def busy(self) -> bool:
        return self._lock.locked()

    def run_in_background(self, paths: Optional[List[str]], source: str, label: str = "", limit: int = 0,
                          spec: Optional[dict] = None) -> bool:
        """在背景跑一批。spec 是網頁挑這一批的條件（手動提取時才有），改「這次最多幾支」時用來補。"""
        if self._lock.locked():
            return False
        self.batch_spec = spec
        threading.Thread(target=self.run, args=(paths, source, label, limit), daemon=True).start()
        return True
