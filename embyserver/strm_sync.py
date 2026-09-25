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

import json

import httpx

from .config import P115StrmConfig, StrmTask
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


# 網頁上設定的同步任務與自動偵測到的伺服器位址，存在資料庫 meta
TASKS_META_KEY = "p115_strm_tasks"
SERVER_URL_META_KEY = "server_url"


def strm_content(cfg: P115StrmConfig, pickcode: str, file_name: str, base_url: Optional[str] = None) -> str:
    """本伺服器的短連結：{base_url}/d/{pickcode}.{副檔名}

    副檔名讓播放器與掃描器認得容器格式；include_name 時再附上 ?/{原檔名} 方便辨識。
    """
    base = base_url or cfg.base_url or "http://127.0.0.1:8096"
    url = f"{base.rstrip('/')}/d/{pickcode}{Path(file_name).suffix.lower()}"
    if cfg.include_name:
        url += f"?/{quote(file_name)}"
    return url


class StrmSync:
    def __init__(
        self,
        p115: P115Service,
        cfg: P115StrmConfig,
        on_done: Optional[Callable[[], None]] = None,
        port: int = 8096,
    ):
        self.p115 = p115
        self.cfg = cfg
        self.port = port
        self.on_done = on_done
        self.result = SyncResult()
        self._lock = threading.Lock()
        self._http = httpx.Client(timeout=60, follow_redirects=True)
        self._stop = threading.Event()

    # ---------------- 設定 ----------------

    @property
    def tasks(self) -> List[StrmTask]:
        """網頁上存過任務就用網頁的，否則用設定檔的 p115.strm.tasks。"""
        raw = self.p115.db.get_meta(TASKS_META_KEY)
        if raw is None:
            return list(self.cfg.tasks)
        return [StrmTask(remote=t["remote"], local=t["local"]) for t in json.loads(raw)]

    def set_tasks(self, tasks: List[StrmTask]) -> None:
        data = [{"remote": t.remote, "local": t.local} for t in tasks]
        self.p115.db.set_meta(TASKS_META_KEY, json.dumps(data, ensure_ascii=False))

    @property
    def base_url(self) -> str:
        """設定檔有填就用設定檔的；沒填就用管理員開網頁時的網址。"""
        return (
            self.cfg.base_url
            or self.p115.db.get_meta(SERVER_URL_META_KEY)
            or f"http://127.0.0.1:{self.port}"
        )

    def remember_base_url(self, url: str) -> None:
        url = url.rstrip("/")
        if url and not self.cfg.base_url and self.p115.db.get_meta(SERVER_URL_META_KEY) != url:
            self.p115.db.set_meta(SERVER_URL_META_KEY, url)

    # ---------------- 執行 ----------------

    def run(self) -> SyncResult:
        if not self._lock.acquire(blocking=False):
            log.info("115 strm 同步已在進行，略過")
            return self.result
        self.result = SyncResult(started=time.time(), running=True)
        try:
            for task in self.tasks:
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
        if self.on_done and self.cfg.scan_after_sync:
            self.on_done()
        return r

    def run_in_background(self) -> bool:
        if self._lock.locked():
            return False
        threading.Thread(target=self.run, daemon=True).start()
        return True

    def start_schedule(self) -> None:
        """定時同步；間隔在網頁上可隨時修改，所以每分鐘檢查一次是否到期。"""

        def loop():
            last = time.time()
            while not self._stop.wait(60):
                interval = self.cfg.interval
                if interval <= 0 or time.time() - last < interval * 60:
                    continue
                last = time.time()
                if self.p115.logged_in and self.tasks:
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
        content = strm_content(self.cfg, info["pickcode"].lower(), info["name"], self.base_url)
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
