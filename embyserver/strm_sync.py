"""從 115 目錄產生 strm（以及下載 nfo／圖片／字幕）。

兩種同步方式：
- 全量：逐層列出 115 目錄，比對所有檔案；可以刪除 115 上已不存在的 strm。
  同時記下每個 115 檔案、資料夾對應到哪個本機路徑（資料表 p115_index），給增量同步用。
- 增量：
  1. 讀 115 生活事件（網盤的操作紀錄）：上傳、接收、複製、移動、改名、刪除。
     生活事件只給檔案 id，靠 p115_index 找到本機原本的位置；移動、改名時把 strm 連同
     同名的 nfo、海報（例如 MoviePilot 刮削的）一起搬過去，刪除時跟著刪（要開 delete_stale）。
  2. 再請 115 依修改時間列出任務目錄裡最近修改的檔案，補抓沒有產生事件的上傳
     （例如離線下載、第三方工具上傳）。
  生活事件需要掃碼或 cookie 登入，只用開放平台時只做第 2 步。任務還沒全量同步過、
  或事件超出 115 保留的範圍時，自動改跑全量；定期全量（預設每週）查漏補缺。
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from .config import P115StrmConfig, StrmTask
from .db import Database
from .p115 import (
    BROWSER_UA, LIFE_COPY_FOLDER, LIFE_DELETE, LIFE_NEW_FOLDER, LIFE_RECEIVE, LIFE_UPLOAD,
    LifeEventGap, P115Error, P115NotFound, P115Service,
)

log = logging.getLogger(__name__)

VIDEO_EXTS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv",
    ".webm", ".rmvb", ".mpg", ".mpeg", ".iso", ".3gp",
}
METADATA_EXTS = {".nfo", ".jpg", ".jpeg", ".png", ".webp", ".srt", ".ass", ".ssa", ".sup", ".vtt"}

# 自動偵測到的伺服器位址、各任務的同步進度，存在資料庫 meta
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
    # 增量同步讀到的生活事件數、依事件搬移（移動、改名）的本機檔案或資料夾數
    events: int = 0
    moved: int = 0
    errors: List[str] = field(default_factory=list)
    # 這次新產生的 strm（本機路徑），同步後交給 MoviePilot 刮削
    new_files: List[str] = field(default_factory=list)
    # 增量同步時改跑全量的任務，以及原因
    fell_back_to_full: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

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


def _remote_root(task: StrmTask) -> str:
    return "/" + task.remote.strip("/")


def _rel(root: str, path: str) -> Optional[str]:
    """115 路徑相對於任務目錄的路徑；任務目錄本身是 ""，不在任務目錄底下是 None。"""
    if root == "/":
        return path.strip("/")
    if path == root:
        return ""
    if path.startswith(root + "/"):
        return path[len(root) + 1:]
    return None


def _belongs(name: str, stem: str) -> bool:
    return name.startswith(stem + ".") or name.startswith(stem + "-")


def _sidecars(folder: Path, stem: str) -> List[Path]:
    """跟著某支影片的中繼資料：X.nfo、X-poster.jpg、X.zh.srt 這類同名檔案。

    同資料夾裡有 X-2.strm 時，X-2.nfo 屬於 X-2 而不是 X。
    """
    if not folder.is_dir():
        return []
    files = [f for f in folder.iterdir() if f.is_file()]
    longer = [f.stem for f in files if f.suffix.lower() == ".strm" and f.stem != stem and f.stem.startswith(stem)]
    return [
        f for f in files
        if f.suffix.lower() in METADATA_EXTS and _belongs(f.name, stem) and not any(_belongs(f.name, o) for o in longer)
    ]


def _has_video(folder: Path) -> bool:
    exts = VIDEO_EXTS | {".strm"}
    return any(p.suffix.lower() in exts for p in folder.rglob("*") if p.is_file())


class _TaskIndex:
    """一個同步任務的 115 檔案／資料夾 id → 本機相對路徑。"""

    def __init__(self, db: Database, key: str):
        self.db, self.key = db, key

    def get(self, file_id: int) -> Optional[Tuple[str, bool]]:
        row = self.db.one("SELECT path, is_dir FROM p115_index WHERE task=? AND file_id=?", (self.key, file_id))
        return (row["path"], bool(row["is_dir"])) if row else None

    def set(self, file_id: int, path: str, is_dir: bool) -> None:
        self.db.execute(
            "INSERT INTO p115_index(task, file_id, is_dir, path) VALUES(?,?,?,?) "
            "ON CONFLICT(task, file_id) DO UPDATE SET is_dir=excluded.is_dir, path=excluded.path",
            (self.key, file_id, int(is_dir), path),
        )

    def delete(self, file_id: int) -> None:
        self.db.execute("DELETE FROM p115_index WHERE task=? AND file_id=?", (self.key, file_id))

    def _tree(self, path: str) -> List:
        return self.db.query(
            "SELECT file_id, path FROM p115_index WHERE task=? AND (path=? OR substr(path, 1, ?)=?)",
            (self.key, path, len(path) + 1, path + "/"),
        )

    def move_tree(self, old: str, new: str) -> None:
        rows = self._tree(old)
        self.db.executemany(
            "UPDATE p115_index SET path=? WHERE task=? AND file_id=?",
            [(new + r["path"][len(old):], self.key, r["file_id"]) for r in rows],
        )

    def delete_tree(self, path: str) -> None:
        self.db.executemany(
            "DELETE FROM p115_index WHERE task=? AND file_id=?", [(self.key, r["file_id"]) for r in self._tree(path)]
        )

    def replace_all(self, rows: List[Tuple[int, str, bool]]) -> None:
        with self.db.lock:
            self.db.conn.execute("DELETE FROM p115_index WHERE task=?", (self.key,))
            self.db.conn.executemany(
                "INSERT OR REPLACE INTO p115_index(task, file_id, is_dir, path) VALUES(?,?,?,?)",
                ((self.key, fid, int(is_dir), path) for fid, path, is_dir in rows),
            )
            self.db.conn.commit()


class _Ctx:
    """處理一個任務時用到的東西。"""

    def __init__(self, task: StrmTask, db: Database):
        self.task = task
        self.root = _remote_root(task)
        self.local = Path(task.local).expanduser()
        self.index = _TaskIndex(db, _task_key(task))


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
        self._dirs: Dict[int, Optional[str]] = {}  # 這次同步查過的 115 目錄路徑；None = 已不存在
        self._latest: Optional[Tuple[int, int]] = None
        self._life_enabled = False

    # ---------------- 設定 ----------------

    @property
    def tasks(self) -> List[StrmTask]:
        """設定檔的 p115.strm.tasks（網頁上修改時會寫回設定檔）。"""
        return list(self.cfg.tasks)

    def prune_index(self) -> None:
        """刪掉的任務不再需要對照表。"""
        keys = {_task_key(t) for t in self.tasks}
        stale = [r["task"] for r in self.p115.db.query("SELECT DISTINCT task FROM p115_index") if r["task"] not in keys]
        self.p115.db.executemany("DELETE FROM p115_index WHERE task=?", [(k,) for k in stale])

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

    # ---------------- 同步進度 ----------------
    # 每個任務：since = 看過的最新修改時間，life_id／life_time = 處理到的生活事件，
    # indexed = 已經建好 p115_index，full_at／incremental_at = 上次同步時間

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
        self._dirs = {}
        self._latest = None
        try:
            self._run(mode)
        finally:
            self.result.running = False
            self.result.finished = time.time()
            self._lock.release()
        r = self.result
        log.info(
            "115 strm %s同步完成：生活事件 %s，搬移 %s，新增/更新 %s（新檔 %s），未變 %s，下載中繼資料 %s，刪除 %s，錯誤 %s",
            "增量" if mode == INCREMENTAL else "全量", r.events, r.moved,
            r.strm_created, len(r.new_files), r.strm_unchanged, r.metadata_downloaded, r.removed, len(r.errors),
        )
        if self.on_done:
            try:
                self.on_done(r)
            except Exception:
                log.exception("同步後續處理失敗")
        return r

    def _run(self, mode: str) -> None:
        states = self._states()
        full: List[StrmTask] = []
        incremental: List[StrmTask] = []
        for task in self.tasks:
            if mode == INCREMENTAL and states.get(_task_key(task), {}).get("indexed"):
                incremental.append(task)
            else:
                if mode == INCREMENTAL:
                    # 還沒全量同步過（或是舊版同步的），先全量一次建立對照表
                    self.result.fell_back_to_full.append(task.remote)
                full.append(task)

        events: Optional[List[dict]] = None
        pointers = [states[_task_key(t)].get("life_id") or 0 for t in incremental]
        if incremental and self.p115.cookies and any(pointers):
            first = min((states[_task_key(t)] for t in incremental if states[_task_key(t)].get("life_id")),
                        key=lambda st: st["life_id"])
            try:
                events = self.p115.life_events(first["life_id"], first.get("life_time") or 0)
                self.result.events = len(events)
            except LifeEventGap as exc:
                self.result.notes.append(f"{exc}，改跑全量")
                for task in incremental:
                    self.result.fell_back_to_full.append(task.remote)
                full += incremental
                incremental = []
            except P115Error as exc:
                # 讀不到事件時仍然用修改時間補抓新檔案，事件下次再讀
                self.result.errors.append(f"讀取 115 生活事件失敗：{exc}")

        for task in full:
            self._guard(task, lambda: self._run_full(task))
        for task in incremental:
            self._guard(task, lambda: self._run_incremental(task, states[_task_key(task)], events))

    def _guard(self, task: StrmTask, job: Callable[[], None]) -> None:
        """一個任務出錯不影響其他任務，錯誤顯示在網頁上。"""
        try:
            job()
        except Exception as exc:
            if not isinstance(exc, P115Error):
                log.exception("115 strm 同步發生未預期的錯誤")
            msg = f"{task.remote}: {exc}"
            log.error("115 strm 同步失敗：%s", msg)
            self.result.errors.append(msg)

    def run_in_background(self, mode: str = FULL) -> bool:
        if self._lock.locked():
            return False
        threading.Thread(target=self.run, args=(mode,), daemon=True).start()
        return True

    def start_schedule(self) -> None:
        """定時同步；間隔在網頁上可隨時修改，所以每分鐘檢查一次是否到期。

        interval（分鐘）跑增量；full_interval（小時）跑全量，依各任務上次全量的時間計算，
        重新啟動不會重新計時。兩者同時到期時只跑全量。
        """

        def loop():
            last_inc = time.time()
            while not self._stop.wait(60):
                if not (self.p115.logged_in and self.tasks):
                    continue
                now = time.time()
                if self._full_due(now):
                    last_inc = now
                    self.run(FULL)
                elif self.cfg.interval > 0 and now - last_inc >= self.cfg.interval * 60:
                    last_inc = now
                    self.run(INCREMENTAL)

        threading.Thread(target=loop, daemon=True).start()

    def _full_due(self, now: float) -> bool:
        if self.cfg.full_interval <= 0:
            return False
        states = self._states()
        done = [states.get(_task_key(t), {}).get("full_at") for t in self.tasks]
        done = [t for t in done if t]
        # 從沒同步過的任務等使用者第一次按同步（或增量同步自動補全量），這裡不搶著跑
        return bool(done) and now - min(done) >= self.cfg.full_interval * 3600

    def _latest_event(self) -> Tuple[int, int]:
        """目前最新的生活事件；全量同步前記下，同步期間發生的事件下次增量再處理。"""
        if self._latest is None:
            self._latest = (0, 0)
            if self.p115.cookies:
                try:
                    if not self._life_enabled:
                        self.p115.enable_life()
                        self._life_enabled = True
                    self._latest = self.p115.latest_life_event()
                except P115Error as exc:
                    log.warning("讀取 115 生活事件失敗，增量同步只能靠修改時間：%s", exc)
                    self.result.notes.append(f"讀不到 115 生活事件（{exc}），這次只依修改時間補抓")
        return self._latest

    def _run_full(self, task: StrmTask) -> None:
        remote, local = task.remote, Path(task.local).expanduser()
        log.info("開始全量同步 115:%s -> %s", remote, local)
        life = self._latest_event()
        cid = self.p115.dir_id(remote)
        local.mkdir(parents=True, exist_ok=True)
        produced: set[str] = set()
        rows: List[Tuple[int, str, bool]] = []
        newest = 0
        for rel, info in self.p115.walk(cid, delay=self.cfg.request_delay, dirs=True):
            if info["is_dir"]:
                rows.append((info["id"], rel, True))
                continue
            newest = max(newest, info.get("mtime") or 0)
            target = self._handle_file(local, rel, info)
            if target:
                produced.add(str(target))
                rows.append((info["id"], target.relative_to(local).as_posix(), False))
        if self.cfg.delete_stale:
            self._remove_stale(local, produced)
        _TaskIndex(self.p115.db, _task_key(task)).replace_all(rows)
        self._save_state(
            task, since=newest or int(time.time()), full_at=int(time.time()), indexed=True,
            life_id=life[0], life_time=life[1],
        )

    def _run_incremental(self, task: StrmTask, state: dict, events: Optional[List[dict]]) -> None:
        ctx = _Ctx(task, self.p115.db)
        ctx.local.mkdir(parents=True, exist_ok=True)
        pointer = state.get("life_id") or 0
        if events is not None and pointer:
            mine = [e for e in events if e["id"] > pointer]
            if mine:
                self._apply_events(ctx, mine)
                self._save_state(task, life_id=mine[-1]["id"], life_time=mine[-1]["mtime"])
        elif not pointer and self.p115.cookies:
            # 之前沒讀過事件（例如只用開放平台）：從現在開始記
            life = self._latest_event()
            if life[0]:
                self._save_state(task, life_id=life[0], life_time=life[1])
        self._scan_recent(ctx, state)

    # ---------------- 增量：生活事件 ----------------

    def _apply_events(self, ctx: _Ctx, events: List[dict]) -> None:
        """同一個檔案只看最後一個事件，那就是它現在的狀態；依事件發生順序處理。"""
        log.info("處理 %s 個 115 生活事件（%s）", len(events), ctx.root)
        latest = sorted({e["file_id"]: e for e in events}.values(), key=lambda e: e["id"])
        for ev in latest:
            try:
                if ev["type"] == LIFE_DELETE:
                    self._event_delete(ctx, ev)
                elif ev["is_dir"]:
                    self._event_dir(ctx, ev)
                else:
                    self._event_file(ctx, ev)
            except P115NotFound:
                continue  # 之後又被刪掉或移走了

    def _remote_dir(self, cid: int) -> Optional[str]:
        if cid not in self._dirs:
            if self.cfg.request_delay:
                time.sleep(self.cfg.request_delay)
            try:
                self._dirs[cid] = self.p115.dir_path(cid)
            except P115NotFound:
                self._dirs[cid] = None
        return self._dirs[cid]

    def _wanted(self, name: str) -> bool:
        suffix = Path(name).suffix.lower()
        return suffix in VIDEO_EXTS or (suffix in METADATA_EXTS and self.cfg.download_metadata)

    def _event_file(self, ctx: _Ctx, ev: dict) -> None:
        old = ctx.index.get(ev["file_id"])
        if not old and not self._wanted(ev["name"]):
            return  # 跟影片無關的檔案，不必查它在哪裡
        rel = None
        parent = self._remote_dir(ev["parent_id"])
        if parent is not None:
            rel = _rel(ctx.root, posixpath.join(parent, ev["name"]))
        info = {"id": ev["file_id"], "name": ev["name"], "pickcode": ev["pickcode"], "size": ev["size"], "mtime": ev["mtime"]}
        if rel and self._place_file(ctx, rel, info):
            return
        if old and self.cfg.delete_stale:
            # 移出任務目錄，或改名成不是影片
            self._delete_local(ctx, old[0], old[1])

    def _event_dir(self, ctx: _Ctx, ev: dict) -> None:
        fid = ev["file_id"]
        old = ctx.index.get(fid)
        current = self._remote_dir(fid)
        rel = _rel(ctx.root, current) if current is not None else None
        if rel == "":
            return  # 任務目錄本身
        if rel is None:
            if old and self.cfg.delete_stale:
                self._delete_local(ctx, old[0], True)
            return
        if old and old[0] != rel:
            self._move_dir(ctx, old[0], rel)
        ctx.index.set(fid, rel, True)
        if (not old and ev["type"] != LIFE_NEW_FOLDER) or ev["type"] in (LIFE_UPLOAD, LIFE_RECEIVE, LIFE_COPY_FOLDER):
            # 新出現在任務目錄裡的資料夾（上傳、接收、複製、從外面移進來）：列出裡面的檔案
            for sub, info in self.p115.walk(fid, rel, self.cfg.request_delay, dirs=True):
                if info["is_dir"]:
                    ctx.index.set(info["id"], sub, True)
                else:
                    self._place_file(ctx, sub, info)

    def _event_delete(self, ctx: _Ctx, ev: dict) -> None:
        old = ctx.index.get(ev["file_id"])
        if old and self.cfg.delete_stale:
            self._delete_local(ctx, old[0], old[1])

    # ---------------- 增量：依修改時間補抓 ----------------

    def _scan_recent(self, ctx: _Ctx, state: dict) -> None:
        since = float(state.get("since") or 0)
        log.info("找 115:%s 裡 %s 之後修改的檔案", ctx.root, time.ctime(since))
        cid = self.p115.dir_id(ctx.root)
        self._dirs.setdefault(cid, ctx.root)
        newest = since
        for info in self.p115.iter_changed_files(cid, since - INCREMENTAL_OVERLAP):
            newest = max(newest, info["mtime"])
            parent = self._remote_dir(info["parent_id"])
            rel = _rel(ctx.root, posixpath.join(parent, info["name"])) if parent is not None else None
            if rel:
                self._place_file(ctx, rel, info)
        self._save_state(ctx.task, since=newest, incremental_at=int(time.time()))

    # ---------------- 本機檔案 ----------------

    def _target(self, rel: str, info: dict) -> Optional[Path]:
        """115 上的檔案在本機對應的相對路徑；不需要的檔案回傳 None。"""
        suffix = Path(rel).suffix.lower()
        if suffix in VIDEO_EXTS:
            if info["size"] < self.cfg.min_size_mb * 1024 * 1024:
                return None
            return Path(rel).with_suffix(".strm")
        if suffix in METADATA_EXTS and self.cfg.download_metadata:
            return Path(rel)
        return None

    def _handle_file(self, local: Path, rel: str, info: dict) -> Optional[Path]:
        """依副檔名產生 strm 或下載中繼資料，回傳本機檔案路徑（略過的檔案回傳 None）。"""
        target = self._target(rel, info)
        if target is None:
            return None
        if target.suffix == ".strm":
            self._write_strm(local / target, info)
        else:
            self._download(local / target, info)
        return local / target

    def _place_file(self, ctx: _Ctx, rel: str, info: dict) -> Optional[Path]:
        """確保 115 上的檔案在本機的正確位置：位置變了就搬（連同刮削資料），沒有就產生。"""
        target = self._target(rel, info)
        if target is None:
            return None
        old = ctx.index.get(info["id"])
        if old and not old[1] and old[0] != target.as_posix():
            self._move_file(ctx, old[0], target.as_posix())
        path = self._handle_file(ctx.local, rel, info)
        ctx.index.set(info["id"], target.as_posix(), False)
        return path

    def _move_file(self, ctx: _Ctx, old: str, new: str) -> None:
        src, dst = ctx.local / old, ctx.local / new
        if not src.is_file():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() == ".strm":
            for f in _sidecars(src.parent, src.stem):
                moved = dst.parent / (dst.stem + f.name[len(src.stem):])
                if not moved.exists():
                    shutil.move(str(f), str(moved))
        shutil.move(str(src), str(dst))
        self.result.moved += 1
        log.info("115 上移動或改名，本機跟著搬：%s -> %s", old, new)
        self._prune(ctx, src.parent)

    def _move_dir(self, ctx: _Ctx, old: str, new: str) -> None:
        src, dst = ctx.local / old, ctx.local / new
        if src.is_dir():
            if dst.exists():
                self._merge_dir(src, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
            self.result.moved += 1
            log.info("115 上移動或改名資料夾，本機跟著搬：%s -> %s", old, new)
            self._prune(ctx, src.parent)
        ctx.index.move_tree(old, new)

    def _merge_dir(self, src: Path, dst: Path) -> None:
        for child in list(src.iterdir()):
            target = dst / child.name
            if child.is_dir() and target.is_dir():
                self._merge_dir(child, target)
            elif not target.exists():
                shutil.move(str(child), str(target))
        try:
            src.rmdir()
        except OSError:
            pass

    def _delete_local(self, ctx: _Ctx, rel: str, is_dir: bool) -> None:
        """115 上刪除或移出任務目錄：刪掉 strm 和它的中繼資料，其他檔案不動。"""
        path = ctx.local / rel
        if is_dir:
            if path.is_dir():
                for f in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                    if f.is_file() and (f.suffix.lower() == ".strm" or f.suffix.lower() in METADATA_EXTS):
                        f.unlink(missing_ok=True)
                        self.result.removed += 1
                    elif f.is_dir():
                        try:
                            f.rmdir()
                        except OSError:
                            pass
                try:
                    path.rmdir()
                except OSError:
                    pass
            ctx.index.delete_tree(rel)
        else:
            if path.suffix.lower() == ".strm":
                for f in _sidecars(path.parent, path.stem):
                    f.unlink(missing_ok=True)
                    self.result.removed += 1
            if path.exists():
                path.unlink()
                self.result.removed += 1
            ctx.index.delete_tree(rel)
        log.info("115 上已刪除或移走，本機跟著刪：%s", rel)
        self._prune(ctx, path.parent)

    def _prune(self, ctx: _Ctx, folder: Path) -> None:
        """由下往上清掉空資料夾；開了 delete_stale 時，已經沒有影片的資料夾裡的中繼資料也一起刪。"""
        while folder != ctx.local and ctx.local in folder.parents:
            if folder.is_dir():
                if self.cfg.delete_stale and not _has_video(folder):
                    for f in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                        if f.is_file() and f.suffix.lower() in METADATA_EXTS:
                            f.unlink(missing_ok=True)
                            self.result.removed += 1
                        elif f.is_dir():
                            try:
                                f.rmdir()
                            except OSError:
                                pass
                try:
                    folder.rmdir()
                except OSError:
                    return  # 還有東西，上層也不會是空的
            folder = folder.parent

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
            for stem in stems:
                for f in _sidecars(folder, stem):
                    if str(f) not in produced and f.exists():
                        f.unlink()
                        self.result.removed += 1
        for dirpath, dirnames, filenames in os.walk(local, topdown=False):
            folder = Path(dirpath)
            if folder == local:
                continue
            if _has_video(folder):
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
