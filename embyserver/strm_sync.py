"""從 115 目錄產生 strm（以及下載 nfo／圖片／字幕），做法參考 p115strmhelper 的全量同步。"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import quote

import httpx

from .config import P115StrmConfig
from .p115 import BROWSER_UA, P115Error, P115Service

log = logging.getLogger(__name__)

VIDEO_EXTS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv",
    ".webm", ".rmvb", ".mpg", ".mpeg", ".iso", ".3gp",
}
METADATA_EXTS = {".nfo", ".jpg", ".jpeg", ".png", ".webp", ".srt", ".ass", ".ssa", ".sup", ".vtt"}


@dataclass
class SyncResult:
    started: float = 0.0
    finished: float = 0.0
    running: bool = False
    strm_created: int = 0
    strm_unchanged: int = 0
    metadata_downloaded: int = 0
    removed: int = 0
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def strm_content(cfg: P115StrmConfig, pickcode: str, file_name: str) -> str:
    """本伺服器的短連結：{base_url}/d/{pickcode}.{副檔名}

    副檔名讓播放器與掃描器認得容器格式；include_name 時再附上 ?/{原檔名} 方便辨識。
    """
    url = f"{cfg.base_url.rstrip('/')}/d/{pickcode}{Path(file_name).suffix.lower()}"
    if cfg.include_name:
        url += f"?/{quote(file_name)}"
    return url


class StrmSync:
    def __init__(self, p115: P115Service, cfg: P115StrmConfig, on_done: Optional[Callable[[], None]] = None):
        self.p115 = p115
        self.cfg = cfg
        self.on_done = on_done
        self.result = SyncResult()
        self._lock = threading.Lock()
        self._http = httpx.Client(timeout=60, follow_redirects=True)
        self._stop = threading.Event()

    # ---------------- 執行 ----------------

    def run(self) -> SyncResult:
        if not self._lock.acquire(blocking=False):
            log.info("115 strm 同步已在進行，略過")
            return self.result
        self.result = SyncResult(started=time.time(), running=True)
        try:
            for task in self.cfg.tasks:
                try:
                    self._run_task(task.remote, Path(task.local).expanduser())
                except P115Error as exc:
                    msg = f"{task.remote}: {exc}"
                    log.error("115 strm 同步失敗：%s", msg)
                    self.result.errors.append(msg)
        finally:
            self.result.running = False
            self.result.finished = time.time()
            self._lock.release()
        r = self.result
        log.info(
            "115 strm 同步完成：新增/更新 %s，未變 %s，下載中繼資料 %s，刪除 %s，錯誤 %s",
            r.strm_created, r.strm_unchanged, r.metadata_downloaded, r.removed, len(r.errors),
        )
        if self.on_done:
            self.on_done()
        return r

    def run_in_background(self) -> bool:
        if self._lock.locked():
            return False
        threading.Thread(target=self.run, daemon=True).start()
        return True

    def start_schedule(self) -> None:
        if self.cfg.interval <= 0 or not self.cfg.tasks:
            return

        def loop():
            while not self._stop.wait(self.cfg.interval * 60):
                if self.p115.logged_in:
                    self.run()

        threading.Thread(target=loop, daemon=True).start()

    def _run_task(self, remote: str, local: Path) -> None:
        log.info("開始同步 115:%s -> %s", remote, local)
        cid = self.p115.dir_id(remote)
        local.mkdir(parents=True, exist_ok=True)
        produced: set[str] = set()
        for rel, info in self.p115.walk(cid, delay=self.cfg.request_delay):
            suffix = Path(rel).suffix.lower()
            if suffix in VIDEO_EXTS:
                if info["size"] < self.cfg.min_size_mb * 1024 * 1024:
                    continue
                target = local / Path(rel).with_suffix(".strm")
                produced.add(str(target))
                self._write_strm(target, info)
            elif suffix in METADATA_EXTS and self.cfg.download_metadata:
                target = local / rel
                produced.add(str(target))
                self._download(target, info)
        if self.cfg.delete_stale:
            self._remove_stale(local, produced)

    def _write_strm(self, target: Path, info: dict) -> None:
        if not info["pickcode"]:
            self.result.errors.append(f"{target.name}: 沒有 pickcode")
            return
        content = strm_content(self.cfg, info["pickcode"].lower(), info["name"])
        try:
            if target.is_file() and target.read_text(encoding="utf-8").strip() == content:
                self.result.strm_unchanged += 1
                return
        except OSError:
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.result.strm_created += 1

    def _download(self, target: Path, info: dict) -> None:
        if target.is_file() and target.stat().st_size == info["size"]:
            return
        try:
            url = self.p115.download_url(info["pickcode"], BROWSER_UA)
            resp = self._http.get(url, headers={"User-Agent": BROWSER_UA})
            resp.raise_for_status()
        except (P115Error, httpx.HTTPError) as exc:
            self.result.errors.append(f"{target.name}: {exc}")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        tmp.write_bytes(resp.content)
        os.replace(tmp, target)
        self.result.metadata_downloaded += 1

    def _remove_stale(self, local: Path, produced: set[str]) -> None:
        """刪除 115 上已不存在的 strm 與中繼資料（只刪本同步會產生的副檔名）。"""
        exts = {".strm"} | (METADATA_EXTS if self.cfg.download_metadata else set())
        for dirpath, _, filenames in os.walk(local):
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix.lower() in exts and str(path) not in produced:
                    path.unlink(missing_ok=True)
                    self.result.removed += 1
