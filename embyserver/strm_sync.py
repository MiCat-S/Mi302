"""從 115 目錄產生 strm（以及下載 nfo／圖片／字幕）。

兩種同步方式：
- 全量：比對任務目錄裡所有檔案；可以刪除 115 上已不存在的 strm。
  1. 用 115 的「導出目錄樹」一次拿到所有資料夾的路徑（只有名稱）；
  2. 一次列出任務目錄底下所有檔案（有 pickcode、大小、所在資料夾 id，每頁 1150 個）；
  3. 用資料夾裡的檔名對出「資料夾 id → 路徑」，對不上的少數資料夾再個別查詢。
  不必逐層列出每個資料夾，資料夾多時快很多。導出、列檔案或查路徑失敗（例如只用開放平台登入、
  115 正在跑別的導出任務）時改回逐層列目錄；目錄樹裡有、115 卻沒列出來的影片不當成已刪除。
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
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote

import httpx

from .config import P115StrmConfig, StrmTask
from .db import Database
from .p115 import (
    LIFE_COPY_FOLDER, LIFE_DELETE, LIFE_NEW_FOLDER, LIFE_RECEIVE, LIFE_UPLOAD, PLAIN_UA,
    LifeEventGap, P115Error, P115NotFound, P115Service, P115Throttled,
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
    # 本機有變動的路徑（新增、更新、搬移前後、刪除），同步後只重新掃描這些地方
    changed: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["new_files"] = len(self.new_files)
        d["changed"] = len(self.changed)
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

    def dirs(self) -> Dict[int, str]:
        return {
            r["file_id"]: r["path"]
            for r in self.db.query("SELECT file_id, path FROM p115_index WHERE task=? AND is_dir=1", (self.key,))
        }

    def files(self) -> Dict[str, int]:
        """檔案的本機相對路徑 → 115 檔案 id。"""
        return {
            r["path"]: r["file_id"]
            for r in self.db.query("SELECT file_id, path FROM p115_index WHERE task=? AND is_dir=0", (self.key,))
        }

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


def match_tree_dirs(
    tree: Iterable[Tuple[str, ...]], files: List[dict], root_cid: int, hints: Optional[Dict[int, str]] = None
) -> Tuple[Dict[int, str], List[int]]:
    """把「資料夾 id → 相對路徑」對出來：目錄樹只有路徑，檔案清單只有所在資料夾的 id。

    一個資料夾 id 裡的每個檔名，在目錄樹裡出現在哪些資料夾，取交集就是它的路徑。
    檔名很普通（例如 01.mkv、movie.nfo）對到好幾個時，先用上次同步記下的 id，再用「已經被別的
    資料夾確定的路徑」排除。回傳 (對照表, 對不上的資料夾 id)；任務目錄本身是 ""。
    """
    parents: Dict[str, Set[str]] = {}
    for parts in tree:
        if parts:
            parents.setdefault(parts[-1], set()).add("/".join(parts[:-1]))
    names: Dict[int, Set[str]] = {}
    for f in files:
        if f["parent_id"] != root_cid:
            names.setdefault(f["parent_id"], set()).add(f["name"])
    candidates: Dict[int, Set[str]] = {}
    for cid, group in names.items():
        found: Optional[Set[str]] = None
        # 少見的檔名先比，通常一兩個就能確定；目錄樹裡沒有的檔名（導出之後才上傳的）不算
        for name in sorted(group, key=lambda n: len(parents.get(n, ()))):
            where = parents.get(name)
            if not where:
                continue
            found = set(where) if found is None else found & where
            if len(found) <= 1:
                break
        candidates[cid] = found or set()
    result: Dict[int, str] = {root_cid: ""}
    for cid, cands in candidates.items():
        if hints and hints.get(cid) in cands:
            result[cid] = hints[cid]
    while True:
        # 扣掉已經確定的路徑只剩一個，而且沒有別的資料夾也剩這一個，才算對上
        taken = set(result.values())
        proposals: Dict[str, List[int]] = {}
        for cid, cands in candidates.items():
            if cid not in result:
                left = cands - taken
                if len(left) == 1:
                    proposals.setdefault(next(iter(left)), []).append(cid)
        unique = {rel: cids[0] for rel, cids in proposals.items() if len(cids) == 1}
        if not unique:
            break
        for rel, cid in unique.items():
            result[cid] = rel
    # 同一個路徑對到兩個資料夾（例如導出之後才複製的資料夾）：分不出誰對，都另外查
    owners: Dict[str, List[int]] = {}
    for cid, rel in result.items():
        owners.setdefault(rel, []).append(cid)
    for cids in owners.values():
        if len(cids) > 1:
            for cid in cids:
                if cid != root_cid:
                    del result[cid]
    return result, [cid for cid in candidates if cid not in result]


def tree_folders(tree: Iterable[Tuple[str, ...]]) -> Set[str]:
    """目錄樹裡底下還有東西的資料夾（相對路徑）。"""
    folders: Set[str] = set()
    for parts in tree:
        for k in range(1, len(parts)):
            folders.add("/".join(parts[:k]))
    return folders


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
        if not self._begin(mode):
            log.info("115 strm 同步已在進行，略過")
            return self.result
        return self._run_started(mode)

    def _begin(self, mode: str) -> bool:
        """佔住同步鎖並換上新的結果，讓呼叫端立刻看得到「同步中」與這次的開始時間。"""
        if not self._lock.acquire(blocking=False):
            return False
        self.result = SyncResult(mode=mode, started=time.time(), running=True)
        self._dirs = {}
        self._latest = None
        return True

    def _run_started(self, mode: str) -> SyncResult:
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
        if self.p115.breaker.tripped:
            # 115 限流或登入失效：再打只會封更久，等冷卻期過了再同步
            self.result.notes.append(self.p115.breaker.message() + "，這次不同步")
            log.warning("115 熔斷中，略過同步：%s", self.p115.breaker.reason)
            return
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
                log.warning("讀取 115 生活事件失敗，這次只用修改時間補抓：%s", exc)

        jobs = [(t, lambda t=t: self._run_full(t)) for t in full]
        jobs += [(t, lambda t=t: self._run_incremental(t, states[_task_key(t)], events)) for t in incremental]
        for i, (task, job) in enumerate(jobs):
            if self.p115.breaker.tripped:
                left = "、".join(t.remote for t, _ in jobs[i:])
                self.result.notes.append(f"{self.p115.breaker.message()}，這些任務這次不同步：{left}")
                break
            self._guard(task, job)

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
        if not self._begin(mode):
            return False
        threading.Thread(target=self._run_started, args=(mode,), daemon=True).start()
        return True

    def start_schedule(self) -> None:
        """定時同步；間隔在網頁上可隨時修改，所以每分鐘檢查一次是否到期。

        interval（分鐘）跑增量；full_interval（小時）跑全量，依各任務上次全量的時間計算，
        重新啟動不會重新計時。兩者同時到期時只跑全量。
        """

        def loop():
            last_inc = time.time()
            while not self._stop.wait(60):
                if not (self.p115.logged_in and self.tasks) or self.p115.breaker.tripped:
                    continue  # 115 熔斷中就等冷卻期過了再排
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
        entries, complete, keep = self._full_entries(task, cid)
        for rel, info in entries:
            if info["is_dir"]:
                rows.append((info["id"], rel, True))
                continue
            newest = max(newest, info.get("mtime") or 0)
            target = self._handle_file(local, rel, info)
            if target:
                produced.add(str(target))
                rows.append((info["id"], target.relative_to(local).as_posix(), False))
        index = _TaskIndex(self.p115.db, _task_key(task))
        if keep:
            # 目錄樹裡有、115 卻沒列出來的影片：strm 和索引都照舊保留
            known = index.files()
            rows += [(known[p], p, False) for p in keep if p in known]
        if self.cfg.delete_stale:
            if complete:
                self._remove_stale(local, produced | {str(local / p) for p in keep})
            else:
                # 有檔案不知道放哪，當成不存在會誤刪
                self.result.notes.append(f"{remote}：有資料夾查不到路徑，這次不刪除本機多出來的 strm")
        index.replace_all(rows)
        self._save_state(
            task, since=newest or int(time.time()), full_at=int(time.time()), indexed=True,
            life_id=life[0], life_time=life[1],
        )

    def _full_entries(self, task: StrmTask, cid: int) -> Tuple[Iterable[Tuple[str, dict]], bool, Set[str]]:
        """全量同步要處理的 (相對路徑, 資訊)、是否每個檔案都知道位置，以及不能當成已刪除的本機 strm（相對路徑）。

        有 cookie 時用導出目錄樹；導出、列檔案或查路徑失敗就改回逐層列目錄。
        """
        if self.p115.cookies:
            try:
                tree = self.p115.export_tree(cid, task.remote)
                found = self._tree_entries(task, cid, tree)
            except P115Throttled:
                raise  # 被限流時改逐層列目錄只會打得更多
            except P115Error as exc:
                log.warning("115 目錄樹比對失敗，改成逐層列目錄：%s", exc)
                self.result.notes.append(f"{task.remote}：目錄樹比對失敗（{exc}），改成逐層列目錄")
            else:
                if found is not None:
                    return found
        return self.p115.walk(cid, delay=self.cfg.request_delay, dirs=True), True, set()

    def _tree_entries(
        self, task: StrmTask, cid: int, tree: List[Tuple[str, ...]]
    ) -> Optional[Tuple[List[Tuple[str, dict]], bool, Set[str]]]:
        files = list(self.p115.iter_changed_files(cid, 0))
        # 列出的影片比目錄樹少很多，代表清單不完整；照這份清單會漏檔、誤刪，改回逐層列目錄
        in_tree = sum(1 for parts in tree if Path(parts[-1]).suffix.lower() in VIDEO_EXTS)
        listed = sum(1 for f in files if Path(f["name"]).suffix.lower() in VIDEO_EXTS)
        if listed < in_tree * 0.9:
            log.warning("115 列出的影片（%s）比目錄樹（%s）少很多，改成逐層列目錄", listed, in_tree)
            self.result.notes.append(f"{task.remote}：115 列出的影片比目錄樹少，改成逐層列目錄")
            return None
        hints = _TaskIndex(self.p115.db, _task_key(task)).dirs()
        dirs, unmatched = match_tree_dirs(tree, files, cid, hints)
        queries = self._resolve_tree_dirs(task, dirs, unmatched, tree_folders(tree), hints)
        missing = {f["parent_id"] for f in files} - dirs.keys()
        log.info(
            "全量同步 115:%s：目錄樹 %s 項、檔案 %s 個、資料夾 %s 個（另外查了 %s 次路徑，%s 個資料夾查不到）",
            task.remote, len(tree), len(files), len(dirs) - 1, queries, len(missing),
        )
        entries: List[Tuple[str, dict]] = [
            (rel, {"is_dir": True, "id": d}) for d, rel in sorted(dirs.items(), key=lambda kv: kv[1]) if rel
        ]
        placed: Set[str] = set()
        for f in files:
            parent = dirs.get(f["parent_id"])
            if parent is not None:
                rel = posixpath.join(parent, f["name"]) if parent else f["name"]
                placed.add(rel)
                entries.append((rel, {**f, "is_dir": False}))
        # 目錄樹裡有、115 卻沒列出來的影片（清單少給了，或導出之後才刪掉）：不能當成已刪除，
        # 這次保留它們的 strm；下次全量兩邊都沒有才刪
        keep = {
            Path("/".join(parts)).with_suffix(".strm").as_posix()
            for parts in tree
            if Path(parts[-1]).suffix.lower() in VIDEO_EXTS and "/".join(parts) not in placed
        }
        if keep:
            log.warning("115 沒列出目錄樹裡的 %s 個影片，這次保留它們的 strm：%s", len(keep), sorted(keep)[:5])
            self.result.notes.append(f"{task.remote}：115 沒列出目錄樹裡的 {len(keep)} 個影片，這次保留它們的 strm 不刪")
        return entries, not missing, keep

    def _resolve_tree_dirs(
        self, task: StrmTask, dirs: Dict[int, str], unmatched: List[int], folders: Set[str], hints: Dict[int, str]
    ) -> int:
        """補齊 dirs：用檔名對不上的資料夾，以及只有子資料夾、沒有檔案的資料夾（例如只放各季的劇集資料夾）。

        後者不影響產生 strm，但增量同步遇到它改名、搬移、刪除時，要靠記下的 id 把本機整個資料夾（連同刮削
        資料）一起搬。每查一個資料夾，115 會一起給它所有上層資料夾的 id，所以一次能補好幾層。回傳查了幾次。
        """
        root = _remote_root(task)
        by_path = {rel: d for d, rel in dirs.items()}
        asked: Set[int] = set()

        def ask(d: int) -> None:
            if d in asked:
                return
            asked.add(d)
            path = ""
            for aid, name in self._remote_ancestors(d) or []:
                path = f"{path}/{name}"
                rel = _rel(root, path)
                if not rel:
                    continue  # 任務目錄本身或更上層
                wrong = by_path.get(rel)
                if wrong is not None and wrong != aid:
                    dirs.pop(wrong, None)  # 115 說這個路徑是別的資料夾：先前用檔名對錯了
                    queue.append(wrong)
                if aid in dirs:
                    by_path.pop(dirs[aid], None)
                dirs[aid] = rel
                by_path[rel] = aid

        def drain() -> None:
            while queue:
                d = queue.pop()
                if d not in dirs:  # 可能已經在查別的資料夾時順便查到了
                    ask(d)

        queue = list(unmatched)
        drain()
        # 只有子資料夾的資料夾：先用上次記下的 id，其餘從它底下已知的資料夾往上查
        for d, rel in hints.items():
            if rel in folders and rel not in by_path and d not in dirs:
                dirs[d] = rel
                by_path[rel] = d
        below: Dict[str, int] = {}
        for rel, d in by_path.items():
            while "/" in rel:
                rel = rel.rsplit("/", 1)[0]
                below.setdefault(rel, d)
        for rel in sorted(folders, key=lambda r: -r.count("/")):
            if rel not in by_path and rel in below:
                ask(below[rel])
        drain()
        return len(asked)

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
            self._remote_ancestors(cid)
        return self._dirs.get(cid)

    def _remote_ancestors(self, cid: int) -> Optional[List[Tuple[int, str]]]:
        """向 115 查資料夾由根往下的每一層 (id, 名稱)；順便記下每一層的路徑。已不存在時回傳 None。"""
        self.p115.breaker.check()  # 逐一查路徑可能很多次，被限流了就別再打
        if self.cfg.request_delay:
            time.sleep(self.cfg.request_delay)
        try:
            chain = self.p115.dir_ancestors(cid)
        except P115NotFound:
            self._dirs[cid] = None
            return None
        path = ""
        for aid, name in chain:
            path = f"{path}/{name}"
            self._dirs[aid] = path
        self._dirs.setdefault(cid, path or "/")
        return chain

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
        self.result.changed += [str(src), str(dst)]
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
            self.result.changed += [str(src), str(dst)]
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
        self.result.changed.append(str(path))
        log.info("115 上已刪除或移走，本機跟著刪：%s", rel)
        self._prune(ctx, path.parent)

    def _prune(self, ctx: _Ctx, folder: Path) -> None:
        """由下往上清掉空資料夾；開了 delete_stale 時，已經沒有影片的資料夾裡的中繼資料也一起刪。"""
        while folder != ctx.local and ctx.local in folder.parents:
            if folder.is_dir():
                if self.cfg.delete_stale and not _has_video(folder):
                    self.result.changed.append(str(folder))
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
            log.warning("115 沒有回傳 pickcode，略過：%s", target)
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
        self.result.changed.append(str(target))
        if not existed:
            self.result.new_files.append(str(target))

    def _download(self, target: Path, info: dict) -> None:
        if target.is_file() and target.stat().st_size == info["size"]:
            return
        try:
            # 115 的 CDN 對 115Browser 的 UA 要 cookie，用一般瀏覽器的 UA
            url = self.p115.download_url(info["pickcode"], PLAIN_UA)
            resp = self._http.get(url, headers=self.p115.file_headers(url, PLAIN_UA))
            resp.raise_for_status()
        except (P115Error, httpx.HTTPError) as exc:
            self.result.errors.append(f"{target.name}: {exc}")
            log.warning("下載 %s 失敗：%s", target.name, exc)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        tmp.write_bytes(resp.content)
        os.replace(tmp, target)
        self.result.metadata_downloaded += 1
        self.result.changed.append(str(target))

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
                    self.result.changed.append(str(path))
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
                    self.result.changed.append(str(f))
            try:
                folder.rmdir()  # 只有空資料夾才刪得掉
            except OSError:
                pass
