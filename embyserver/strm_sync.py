"""從 115 目錄產生 strm（以及下載 nfo／圖片／字幕）。

兩種同步方式：
- 全量：逐層列出 115 目錄，比對所有檔案；可以刪除 115 上已不存在的 strm。
- 增量：請 115 依修改時間由新到舊列出任務目錄底下的所有檔案，只處理上次同步之後
  新增、改名或移入的檔案，API 呼叫次數跟新檔案數量有關，跟片庫大小無關。
  增量同步看不到刪除，所以定期還是要跑一次全量。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import quote

import httpx

from .config import P115StrmConfig, StrmTask
from .p115 import BROWSER_UA, P115Error, P115Service

log = logging.getLogger(__name__)

VIDEO_EXTS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv",
    ".webm", ".rmvb", ".mpg", ".mpeg", ".iso", ".3gp",
}
METADATA_EXTS = {".nfo", ".jpg", ".jpeg", ".png", ".webp", ".srt", ".ass", ".ssa", ".sup", ".vtt"}

# 網頁上設定的同步任務、自動偵測到的伺服器位址、各任務的增量同步進度，存在資料庫 meta
TASKS_META_KEY = "p115_strm_tasks"
SERVER_URL_META_KEY = "server_url"
STATE_META_KEY = "p115_sync_state"
# 增量同步往回多看一段時間，避免 115 與本機時間差或同一秒上傳的檔案漏掉；重複處理只會判定為「未變」
INCREMENTAL_OVERLAP = 600

FULL = "full"
INCREMENTAL = "incremental"


@dataclass
class SyncResult:
    mode: str = ""
    started: float = 0.0
    finished: float = 0.0
    running: bool = False
    strm_created: int = 0
    strm_unchanged: int = 0
    metadata_downloaded: int = 0
    removed: int = 0
    errors: List[str] = field(default_factory=list)
    # 這次新產生的 strm（本機路徑），同步後交給 MoviePilot 刮削
    new_files: List[str] = field(default_factory=list)
    # 沒有增量進度、改跑全量的任務
    fell_back_to_full: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["new_files"] = len(self.new_files)
        return d


def strm_content(cfg: P115StrmConfig, pickcode: str, file_name: str, base_url: Optional[str] = None) -> str:
    """本伺服器的短連結：{base_url}/d/{pickcode}.{副檔名}

    副檔名讓播放器與掃描器認得容器格式；include_name 時再附上 ?/{原檔名} 方便辨識。
    """
    base = base_url or cfg.base_url or "http://127.0.0.1:8096"
    url = f"{base.rstrip('/')}/d/{pickcode}{Path(file_name).suffix.lower()}"
    if cfg.include_name:
        url += f"?/{quote(file_name)}"
    return url


def _task_key(task: StrmTask) -> str:
    return f"{task.remote}\n{task.local}"


class StrmSync:
    def __init__(
        self,
        p115: P115Service,
        cfg: P115StrmConfig,
        on_done: Optional[Callable[[SyncResult], None]] = None,
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

    # ---------------- 增量進度 ----------------

    def _states(self) -> Dict[str, dict]:
        try:
            return json.loads(self.p115.db.get_meta(STATE_META_KEY) or "{}")
        except ValueError:
            return {}

    def _save_state(self, task: StrmTask, **values) -> None:
        states = self._states()
        states.setdefault(_task_key(task), {}).update(values)
        self.p115.db.set_meta(STATE_META_KEY, json.dumps(states))

    def task_states(self) -> List[dict]:
        """每個任務上次全量、增量同步的時間，給網頁顯示。"""
        states = self._states()
        return [states.get(_task_key(t), {}) for t in self.tasks]

    # ---------------- 執行 ----------------

    def run(self, mode: str = FULL) -> SyncResult:
        if not self._lock.acquire(blocking=False):
            log.info("115 strm 同步已在進行，略過")
            return self.result
        self.result = SyncResult(mode=mode, started=time.time(), running=True)
        try:
            for task in self.tasks:
                try:
                    state = self._states().get(_task_key(task))
                    if mode == INCREMENTAL and state and state.get("since"):
                        self._run_incremental(task, state)
                    else:
                        if mode == INCREMENTAL:
                            # 第一次增量同步還沒有基準點，先全量一次
                            self.result.fell_back_to_full.append(task.remote)
                        self._run_full(task)
                except Exception as exc:  # 一個任務出錯不影響其他任務，錯誤顯示在網頁上
                    if not isinstance(exc, P115Error):
                        log.exception("115 strm 同步發生未預期的錯誤")
                    msg = f"{task.remote}: {exc}"
                    log.error("115 strm 同步失敗：%s", msg)
                    self.result.errors.append(msg)
        finally:
            self.result.running = False
            self.result.finished = time.time()
            self._lock.release()
        r = self.result
        log.info(
            "115 strm %s同步完成：新增/更新 %s（新檔 %s），未變 %s，下載中繼資料 %s，刪除 %s，錯誤 %s",
            "增量" if mode == INCREMENTAL else "全量",
            r.strm_created, len(r.new_files), r.strm_unchanged, r.metadata_downloaded, r.removed, len(r.errors),
        )
        if self.on_done:
            try:
                self.on_done(r)
            except Exception:
                log.exception("同步後續處理失敗")
        return r

    def run_in_background(self, mode: str = FULL) -> bool:
        if self._lock.locked():
            return False
        threading.Thread(target=self.run, args=(mode,), daemon=True).start()
        return True

    def start_schedule(self) -> None:
        """定時同步；間隔在網頁上可隨時修改，所以每分鐘檢查一次是否到期。

        interval（分鐘）跑增量，full_interval（小時）跑全量；兩者同時到期時只跑全量。
        """

        def loop():
            last_inc = last_full = time.time()
            while not self._stop.wait(60):
                now = time.time()
                full_due = self.cfg.full_interval > 0 and now - last_full >= self.cfg.full_interval * 3600
                inc_due = self.cfg.interval > 0 and now - last_inc >= self.cfg.interval * 60
                if not (full_due or inc_due):
                    continue
                if not (self.p115.logged_in and self.tasks):
                    continue
                if full_due:
                    last_full = last_inc = now
                    self.run(FULL)
                else:
                    last_inc = now
                    self.run(INCREMENTAL)

        threading.Thread(target=loop, daemon=True).start()

    def _run_full(self, task: StrmTask) -> None:
        remote, local = task.remote, Path(task.local).expanduser()
        log.info("開始全量同步 115:%s -> %s", remote, local)
        cid = self.p115.dir_id(remote)
        local.mkdir(parents=True, exist_ok=True)
        produced: set[str] = set()
        newest = 0
        for rel, info in self.p115.walk(cid, delay=self.cfg.request_delay):
            newest = max(newest, info.get("mtime") or 0)
            target = self._handle_file(local, rel, info)
            if target:
                produced.add(str(target))
        if self.cfg.delete_stale:
            self._remove_stale(local, produced)
        # 下一次增量同步從這次看到的最新修改時間開始找
        self._save_state(task, since=newest or int(time.time()), full_at=int(time.time()))

    def _run_incremental(self, task: StrmTask, state: dict) -> None:
        remote, local = "/" + task.remote.strip("/"), Path(task.local).expanduser()
        since = float(state["since"])
        log.info("開始增量同步 115:%s -> %s（找 %s 之後修改的檔案）", remote, local, time.ctime(since))
        cid = self.p115.dir_id(remote)
        local.mkdir(parents=True, exist_ok=True)
        root = remote.rstrip("/") or "/"
        dir_paths: Dict[int, str] = {cid: root}
        newest = since
        for info in self.p115.iter_changed_files(cid, since - INCREMENTAL_OVERLAP):
            newest = max(newest, info["mtime"])
            parent_id = info["parent_id"]
            if parent_id not in dir_paths:
                if self.cfg.request_delay:
                    time.sleep(self.cfg.request_delay)
                dir_paths[parent_id] = self.p115.dir_path(parent_id)
            parent = dir_paths[parent_id]
            if parent != root and not parent.startswith(root.rstrip("/") + "/"):
                continue  # 理論上不會發生：115 只列出任務目錄底下的檔案
            rel = f"{parent[len(root):].strip('/')}/{info['name']}".strip("/")
            self._handle_file(local, rel, info)
        self._save_state(task, since=newest, incremental_at=int(time.time()))

    def _handle_file(self, local: Path, rel: str, info: dict) -> Optional[Path]:
        """依副檔名產生 strm 或下載中繼資料，回傳本機檔案路徑（略過的檔案回傳 None）。"""
        suffix = Path(rel).suffix.lower()
        if suffix in VIDEO_EXTS:
            if info["size"] < self.cfg.min_size_mb * 1024 * 1024:
                return None
            target = local / Path(rel).with_suffix(".strm")
            self._write_strm(target, info)
            return target
        if suffix in METADATA_EXTS and self.cfg.download_metadata:
            target = local / rel
            self._download(target, info)
            return target
        return None

    def _write_strm(self, target: Path, info: dict) -> None:
        if not info["pickcode"]:
            self.result.errors.append(f"{target.name}: 沒有 pickcode")
            return
        content = strm_content(self.cfg, info["pickcode"].lower(), info["name"], self.base_url)
        existed = target.is_file()
        try:
            if existed and target.read_text(encoding="utf-8").strip() == content:
                self.result.strm_unchanged += 1
                return
        except OSError:
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.result.strm_created += 1
        if not existed:
            self.result.new_files.append(str(target))

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
        """刪除 115 上已不存在的 strm，以及跟著它的中繼資料。

        MoviePilot 刮削產生的 nfo／圖片不在 115 上，所以中繼資料不能只看「這次有沒有從 115 下載」：
        只刪掉與被刪 strm 同名的檔案（例如 X.nfo、X-poster.jpg、X.zh.srt），
        以及底下已經沒有任何影片的資料夾裡的中繼資料。其他副檔名的檔案一律不動。
        """
        gone_stems: Dict[Path, List[str]] = {}
        for dirpath, _, filenames in os.walk(local):
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix.lower() == ".strm" and str(path) not in produced:
                    path.unlink(missing_ok=True)
                    self.result.removed += 1
                    gone_stems.setdefault(path.parent, []).append(path.stem)
        for folder, stems in gone_stems.items():
            for f in folder.iterdir():
                if f.is_file() and f.suffix.lower() in METADATA_EXTS and str(f) not in produced:
                    if any(f.name.startswith(stem + ".") or f.name.startswith(stem + "-") for stem in stems):
                        f.unlink(missing_ok=True)
                        self.result.removed += 1
        video_exts = VIDEO_EXTS | {".strm"}
        for dirpath, dirnames, filenames in os.walk(local, topdown=False):
            folder = Path(dirpath)
            if folder == local:
                continue
            has_video = any(p.suffix.lower() in video_exts for p in folder.rglob("*") if p.is_file())
            if has_video:
                continue
            for name in filenames:
                f = folder / name
                if f.suffix.lower() in METADATA_EXTS and str(f) not in produced:
                    f.unlink(missing_ok=True)
                    self.result.removed += 1
            try:
                folder.rmdir()  # 只有空資料夾才刪得掉
            except OSError:
                pass
