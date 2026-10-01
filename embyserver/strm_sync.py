"""從 115 目錄產生 strm（以及下載 nfo／圖片／字幕）。

詳細說明見 docs/modules.md 的「embyserver/strm_sync.py」。
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote, unquote

import httpx

from .config import P115StrmConfig, StrmTask
from .db import Database
from .filetypes import LIBRARY_VIDEO_EXTS, METADATA_EXTS, VIDEO_EXTS
from .mediainfo import SIDECAR_SUFFIX as MEDIAINFO_SUFFIX
from .mediainfo import sidecar_path as mediainfo_sidecar
from .p115 import (
    LIFE_COPY_FOLDER, LIFE_DELETE, LIFE_NEW_FOLDER, LIFE_RECEIVE, LIFE_UPLOAD, PLAIN_UA,
    LifeEventGap, P115Error, P115NotFound, P115Service, P115Throttled, extract_pickcode,
)
from .workers import Stopped, Workers

log = logging.getLogger(__name__)


# 自動偵測到的伺服器位址、各任務的同步進度，存在資料庫 meta
SERVER_URL_META_KEY = "server_url"
STATE_META_KEY = "p115_sync_state"
# 增量同步往回多看一段時間，避免 115 與本機時間差或同一秒上傳的檔案漏掉；重複處理只會判定為「未變」
INCREMENTAL_OVERLAP = 600
# 全量同步：115 一次列出的影片少於目錄樹裡的這個比例，代表清單不完整，改成逐層列目錄
MIN_LISTED_RATIO = 0.9
# 跟著刪：115 上沒有、同步紀錄裡也沒有的 strm 超過這麼多個、又超過本機的這個比例時不刪（多半是 115 目錄填錯了）
STALE_GUARD = 100
STALE_GUARD_RATIO = 0.5
# 中繼資料（nfo、圖片、字幕）超過這麼大的不下載：正常的不會這麼大，多半是取錯副檔名的檔案
METADATA_MAX_BYTES = 64 << 20
# 下載中繼資料時，兩次向 115 取直鏈至少間隔幾秒（request_delay 比較大就照它；設成 0 是使用者明說不要間隔）
METADATA_PACE = 0.5

FULL = "full"
INCREMENTAL = "incremental"


@dataclass
class SyncResult:
    mode: str = ""
    started: float = 0.0
    finished: float = 0.0
    running: bool = False
    stopped: bool = False  # 按了停止：沒做完的任務進度不存，下次同步再補
    strm_created: int = 0
    strm_unchanged: int = 0
    metadata_downloaded: int = 0
    metadata_skipped: int = 0  # 115 限流時先不下載的中繼資料（strm 照常產生），下次全量同步再補
    removed: int = 0
    # 增量同步讀到的生活事件數、依事件搬移（移動、改名）的本機檔案或資料夾數
    events: int = 0
    moved: int = 0
    errors: List[str] = field(default_factory=list)
    # 這次新產生的 strm（本機路徑），同步後交給 MoviePilot 刮削
    new_files: List[str] = field(default_factory=list)
    # 115 上的檔案被換掉（pickcode 變了）的 strm：舊的媒體資訊已作廢，要重新探測
    replaced: List[str] = field(default_factory=list)
    # 增量同步時改跑全量的任務，以及原因
    fell_back_to_full: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    # 本機有變動的路徑（新增、更新、搬移前後、刪除），同步後只重新掃描這些地方
    changed: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["new_files"] = len(self.new_files)
        d["replaced"] = len(self.replaced)
        d["changed"] = len(self.changed)
        return d


# Mi302 產生的 strm：{伺服器網址}/d/{pickcode}.{副檔名}，可能再附 ?/{原檔名}；伺服器網址可以帶路徑（反向代理的子路徑）
OWN_STRM_RE = re.compile(r"^https?://\S+?/d/([A-Za-z0-9]{17})(\.[A-Za-z0-9]{1,5})?(?:\?/(.*))?$")


@dataclass
class RewriteResult:
    """把現有 strm 改成新的伺服器網址（或附不附原檔名）的結果。"""

    running: bool = False
    started: float = 0.0
    finished: float = 0.0
    base_url: str = ""  # 改成哪個網址
    rewritten: int = 0
    unchanged: int = 0
    skipped: int = 0  # 不是 Mi302 產生的 strm（別的工具的網址、本機路徑），不動
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["errors"] = self.errors[:20]
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


def task_key(task: StrmTask) -> str:
    """同步任務在資料庫裡的鍵（p115_index.task、同步進度）：115 目錄和本機資料夾，中間隔一個換行。"""
    return f"{task.remote}\n{task.local}"


def remote_root(task: StrmTask) -> str:
    """同步任務的 115 目錄，統一成「/開頭、結尾沒有 /」（根目錄是 /）。"""
    return "/" + task.remote.strip("/")


def outer_roots(tasks: Iterable[StrmTask]) -> List[str]:
    """同步任務的 115 目錄；互相包含的只留外層，免得同一個檔案、資料夾看兩次。"""
    out: List[str] = []
    for root in sorted({remote_root(t) for t in tasks}, key=len):
        if not any(root == o or root.startswith(o.rstrip("/") + "/") for o in out):
            out.append(root)
    return out


def safe_rel(rel: str) -> bool:
    """115 上的名稱組出來的相對路徑能不能直接接在本機的任務資料夾後面：每一層都不能是空的、「.」、「..」
    （local / "../x.nfo" 會寫到、刪到任務資料夾外面），不能有 NUL；Windows 上反斜線也是分隔符號。"""
    parts = rel.split("/")
    return bool(rel) and not any(p in ("", ".", "..") or "\x00" in p or (os.sep == "\\" and "\\" in p) for p in parts)


def _rel(root: str, path: str) -> Optional[str]:
    """115 路徑相對於任務目錄的路徑；任務目錄本身是 ""，不在任務目錄底下是 None。
    會跑出本機任務資料夾的（某一層叫「..」）也當成不在任務目錄裡。"""
    if root == "/":
        rel: Optional[str] = path.strip("/")
    elif path == root:
        rel = ""
    elif path.startswith(root + "/"):
        rel = path[len(root) + 1:]
    else:
        return None
    return rel if not rel or safe_rel(rel) else None


def _belongs(name: str, stem: str) -> bool:
    return name.startswith(stem + ".") or name.startswith(stem + "-")


def _is_metadata(f: Path) -> bool:
    """跟著影片的附屬檔：nfo、圖片、字幕，以及媒體資訊 X-mediainfo.json（不從 115 下載，只在本機跟著搬、跟著刪）。"""
    return f.suffix.lower() in METADATA_EXTS or f.name.lower().endswith(MEDIAINFO_SUFFIX)


def _clear_tree(path: Path) -> Tuple[int, List[str]]:
    """刪掉資料夾裡 Mi302 產生的東西（strm 和中繼資料，其他檔案不動），再刪空的子資料夾和它自己。
    回傳 (刪了幾個檔案, 刪掉的 strm 路徑)。"""
    count, strms = 0, []
    if not path.is_dir():
        return count, strms
    for f in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if f.is_file() and (f.suffix.lower() == ".strm" or _is_metadata(f)):
            if f.suffix.lower() == ".strm":
                strms.append(str(f))
            f.unlink(missing_ok=True)
            count += 1
        elif f.is_dir():
            try:
                f.rmdir()
            except OSError:
                pass
    try:
        path.rmdir()
    except OSError:
        pass
    return count, strms


def _sidecars(folder: Path, stem: str) -> List[Path]:
    """跟著某支影片的中繼資料：X.nfo、X-poster.jpg、X.zh.srt、X-mediainfo.json 這類同名檔案。

    同資料夾裡有 X-2.strm 時，X-2.nfo 屬於 X-2 而不是 X。
    """
    if not folder.is_dir():
        return []
    files = [f for f in folder.iterdir() if f.is_file()]
    longer = [f.stem for f in files if f.suffix.lower() == ".strm" and f.stem != stem and f.stem.startswith(stem)]
    return [
        f for f in files
        if _is_metadata(f) and _belongs(f.name, stem) and not any(_belongs(f.name, o) for o in longer)
    ]


def _has_video(folder: Path) -> bool:
    return any(p.suffix.lower() in LIBRARY_VIDEO_EXTS for p in folder.rglob("*") if p.is_file())


def _same_file(a: Path, b: Path) -> bool:
    """兩個路徑是不是磁碟上同一個檔案（大小寫不分的檔案系統上，只差大小寫的名字就是）。"""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


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
    candidates = _dir_candidates(tree, files, root_cid)
    result: Dict[int, str] = {root_cid: ""}
    for cid, cands in candidates.items():
        if hints and hints.get(cid) in cands:
            result[cid] = hints[cid]
    _settle_unique(candidates, result)
    _drop_shared_paths(result, root_cid)
    return result, [cid for cid in candidates if cid not in result]


def _dir_candidates(tree: Iterable[Tuple[str, ...]], files: List[dict], root_cid: int) -> Dict[int, Set[str]]:
    """每個資料夾 id 可能的路徑：它裡面的檔名在目錄樹裡出現的資料夾取交集。"""
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
    return candidates


def _settle_unique(candidates: Dict[int, Set[str]], result: Dict[int, str]) -> None:
    """反覆排除：扣掉已經確定的路徑只剩一個，而且沒有別的資料夾也剩這一個，才算對上。"""
    while True:
        taken = set(result.values())
        proposals: Dict[str, List[int]] = {}
        for cid, cands in candidates.items():
            if cid not in result:
                left = cands - taken
                if len(left) == 1:
                    proposals.setdefault(next(iter(left)), []).append(cid)
        unique = {rel: cids[0] for rel, cids in proposals.items() if len(cids) == 1}
        if not unique:
            return
        for rel, cid in unique.items():
            result[cid] = rel


def _drop_shared_paths(result: Dict[int, str], root_cid: int) -> None:
    """同一個路徑對到兩個資料夾（例如導出之後才複製的資料夾）：分不出誰對，都另外查。"""
    owners: Dict[str, List[int]] = {}
    for cid, rel in result.items():
        owners.setdefault(rel, []).append(cid)
    for cids in owners.values():
        if len(cids) > 1:
            for cid in cids:
                if cid != root_cid:
                    del result[cid]


def _known_below(by_path: Dict[str, int]) -> Dict[str, int]:
    """每個上層資料夾 → 它底下任一個已知 id 的資料夾（向 115 查那個就能順便知道上層的 id）。"""
    below: Dict[str, int] = {}
    for rel, d in by_path.items():
        while "/" in rel:
            rel = rel.rsplit("/", 1)[0]
            below.setdefault(rel, d)
    return below


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
        self.root = remote_root(task)
        self.local = Path(task.local).expanduser()
        self.index = _TaskIndex(db, task_key(task))
        # 刪除、搬走後可能空掉的資料夾；等這一輪事件全處理完再清，
        # 「先刪舊集再上傳新集」中間那一刻資料夾沒有影片，不能就把 nfo、海報清掉
        self.to_prune: Set[Path] = set()


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
        self.workers = Workers(self._stop)  # 同步、定時同步、改寫 strm 的執行緒；程式結束時等它們停下
        self.rewrite_result = RewriteResult()
        self._rewriting = threading.Lock()
        self._format = (cfg.base_url, cfg.include_name)  # 現有 strm 用的格式；設定改了就改寫
        self._dirs: Dict[int, Optional[str]] = {}  # 這次同步查過的 115 目錄路徑；None = 已不存在
        self._latest: Optional[Tuple[int, int]] = None
        self._life_enabled = False
        self.metadata_pace = METADATA_PACE
        self._last_fetch = 0.0  # 上次為了下載中繼資料向 115 取直鏈的時間（單調時鐘）

    # ---------------- 設定 ----------------

    @property
    def tasks(self) -> List[StrmTask]:
        """設定檔的 p115.strm.tasks（網頁上修改時會寫回設定檔）。"""
        return list(self.cfg.tasks)

    def prune_index(self) -> None:
        """刪掉的任務不再需要對照表。"""
        keys = {task_key(t) for t in self.tasks}
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

    # ---------------- 改寫現有 strm ----------------

    def follow_format(self) -> bool:
        """設定裡的 strm 伺服器網址或「附上原檔名」改了：在背景把現有的 strm 一起改過去。"""
        now = (self.cfg.base_url, self.cfg.include_name)
        if now == self._format:
            return False
        self._format = now
        return self.rewrite_in_background()

    def rewrite_in_background(self) -> bool:
        if self._rewriting.locked():
            return False
        self.rewrite_result = RewriteResult(running=True, started=time.time(), base_url=self.base_url)
        self.workers.start(self.rewrite_strm)
        return True

    def rewrite_strm(self) -> RewriteResult:
        """把各同步任務資料夾裡 Mi302 產生的 strm 改成目前的伺服器網址和格式。

        只讀寫本機檔案、不連 115：pickcode 和副檔名照舊，所以媒體資訊照用，也不必重新掃描。
        別的工具產生的 strm（其他網址、本機路徑）不動；那些要換開頭的話用 redirect.path_rules。
        正在同步時等它做完再改，免得兩邊同時寫同一個檔案。
        """
        with self._rewriting:
            r = self.rewrite_result
            if not r.running:
                r = self.rewrite_result = RewriteResult(running=True, started=time.time())
            with self._lock:
                r.base_url = self.base_url
                try:
                    for task in self.tasks:
                        local = Path(task.local).expanduser()
                        for dirpath, _, filenames in os.walk(local):
                            for name in filenames:
                                if name.lower().endswith(".strm"):
                                    self._rewrite_one(Path(dirpath) / name, r)
                finally:
                    r.running = False
                    r.finished = time.time()
            log.info("現有 strm 改成 %s：改了 %s 個，不用改 %s 個，不是 Mi302 的 %s 個，失敗 %s 個",
                     r.base_url, r.rewritten, r.unchanged, r.skipped, len(r.errors))
            return r

    def _rewrite_one(self, path: Path, r: RewriteResult) -> None:
        try:
            old = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            r.errors.append(f"{path.name}：{exc}")
            return
        m = OWN_STRM_RE.match(old)
        if not m:
            r.skipped += 1
            return
        pickcode, ext, named = m.group(1).lower(), m.group(2) or "", m.group(3)
        # 原檔名：附在網址後面的那一段；沒有的話，strm 的檔名本來就是原檔名換掉副檔名
        file_name = unquote(named) if named else path.stem + ext
        new = strm_content(self.cfg, pickcode, file_name, self.base_url)
        if new == old:
            r.unchanged += 1
            return
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(new, encoding="utf-8")
            os.replace(tmp, path)
            r.rewritten += 1
        except OSError as exc:
            r.errors.append(f"{path.name}：{exc}")

    # ---------------- 115 上刪掉了（例如刪重複） ----------------

    def remove_local(self, file_ids: Iterable[int]) -> List[str]:
        """115 上刪掉了這些檔案（或資料夾）：本機的 strm 和同名的中繼資料一起刪、清掉對照表，回傳刪掉的 strm 路徑。

        資料夾只刪裡面的 strm 和中繼資料，其他檔案不動。不看「跟著刪」的設定，因為是使用者自己在 Mi302 刪的。
        正在同步時等同步做完。
        """
        ids = [int(i) for i in file_ids]
        removed: List[str] = []
        with self._lock:
            for task in self.tasks:
                ctx = _Ctx(task, self.p115.db)
                for fid in ids:
                    old = ctx.index.get(fid)
                    if not old:
                        continue
                    path = ctx.local / old[0]
                    if old[1]:
                        removed += _clear_tree(path)[1]
                        ctx.index.delete_tree(old[0])
                        continue
                    if path.suffix.lower() == ".strm":
                        for f in _sidecars(path.parent, path.stem):
                            f.unlink(missing_ok=True)
                    path.unlink(missing_ok=True)
                    ctx.index.delete_tree(old[0])
                    removed.append(str(path))
                    try:
                        path.parent.rmdir()  # 只有空資料夾才刪得掉
                    except OSError:
                        pass
        return removed

    def clear_local_dir(self, path: str) -> List[str]:
        """115 上已經刪掉的資料夾在本機對應的位置（同步紀錄裡不一定有）：裡面 Mi302 產生的 strm 和中繼資料刪掉，
        其他檔案不動，空了就連資料夾一起刪。回傳刪掉的 strm 路徑。正在同步時等同步做完。"""
        with self._lock:
            return _clear_tree(Path(path))[1]

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
        states.setdefault(task_key(task), {}).update(values)
        self.p115.db.set_meta(STATE_META_KEY, json.dumps(states))

    def task_states(self) -> List[dict]:
        """每個任務上次全量、增量同步的時間，給網頁顯示。"""
        states = self._states()
        return [states.get(task_key(t), {}) for t in self.tasks]

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
        self.workers.cancel.clear()
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
            if mode == INCREMENTAL and states.get(task_key(task), {}).get("indexed"):
                incremental.append(task)
            else:
                if mode == INCREMENTAL:
                    # 還沒全量同步過（或是舊版同步的），先全量一次建立對照表
                    self.result.fell_back_to_full.append(task.remote)
                full.append(task)

        events: Optional[List[dict]] = None
        pointers = [states[task_key(t)].get("life_id") or 0 for t in incremental]
        if incremental and self.p115.cookies and any(pointers):
            first = min((states[task_key(t)] for t in incremental if states[task_key(t)].get("life_id")),
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
        jobs += [(t, lambda t=t: self._run_incremental(t, states[task_key(t)], events)) for t in incremental]
        for i, (task, job) in enumerate(jobs):
            if self.p115.breaker.tripped:
                left = "、".join(t.remote for t, _ in jobs[i:])
                self.result.notes.append(f"{self.p115.breaker.message()}，這些任務這次不同步：{left}")
                break
            if self.workers.halted:
                self.result.stopped = self.workers.by_user
                left = "、".join(t.remote for t, _ in jobs[i:])
                self.result.notes.append(f"{'按了停止' if self.result.stopped else '程式要結束'}，這些任務這次不同步：{left}")
                break
            self._guard(task, job)

    def _guard(self, task: StrmTask, job: Callable[[], None]) -> None:
        """一個任務出錯不影響其他任務，錯誤顯示在網頁上。"""
        try:
            job()
        except Stopped:
            # 進度（since、生活事件）還沒存，也不刪沒列到的 strm；沒做完的下次同步重做，重做只會判定為「未變」
            self.result.stopped = self.result.stopped or self.workers.by_user
            why = "按了停止" if self.workers.by_user else "程式要結束"
            self.result.notes.append(f"{task.remote}：{why}，同步中途停下，下次同步再補")
        except Exception as exc:
            if not isinstance(exc, P115Error):
                log.exception("115 strm 同步發生未預期的錯誤")
            msg = f"{task.remote}: {exc}"
            log.error("115 strm 同步失敗：%s", msg)
            self.result.errors.append(msg)

    def run_in_background(self, mode: str = FULL) -> bool:
        if not self._begin(mode):
            return False
        self.workers.start(self._run_started, mode)
        return True

    def start_schedule(self) -> None:
        """定時同步；間隔在網頁上可隨時修改，所以每分鐘檢查一次是否到期。

        interval（分鐘）跑增量；full_interval（小時）跑全量，依各任務上次全量的時間計算，
        重新啟動不會重新計時。兩者同時到期時只跑全量。
        """

        def loop():
            last_inc = time.time()
            while not self._stop.wait(60):
                try:
                    if not (self.p115.logged_in and self.tasks) or self.p115.breaker.tripped:
                        continue  # 115 熔斷中就等冷卻期過了再排
                    now = time.time()
                    if self._full_due(now):
                        last_inc = now
                        self.run(FULL)
                    elif self.cfg.interval > 0 and now - last_inc >= self.cfg.interval * 60:
                        last_inc = now
                        self.run(INCREMENTAL)
                except Exception:
                    # 任務裡的錯誤 _guard 會接住；這裡接的是任務以外的（資料庫暫時寫不進去、115 回了看不懂的事件…）。
                    # 不接的話這條執行緒就結束了，之後都不會再自動同步，網頁上也看不出來
                    log.exception("定時同步這一輪出錯，下一輪照常")

        self.workers.start(loop)

    def cancel(self) -> bool:
        """按了停止：這一次同步在兩個檔案之間停下，進度不存、不刪 strm，下次同步再補；定時同步照舊。
        沒在同步回傳 False。"""
        if not self.result.running:
            return False
        self.workers.cancel.set()
        return True

    def stop(self) -> None:
        """程式關閉時：停掉定時同步；正在跑的那一次在兩個檔案之間停下（進度不存，下次再補）。"""
        self._stop.set()

    def close(self) -> None:
        """程式結束時關掉下載 nfo、圖片用的連線池。"""
        self._http.close()

    def _full_due(self, now: float) -> bool:
        if self.cfg.full_interval <= 0:
            return False
        states = self._states()
        done = [states.get(task_key(t), {}).get("full_at") for t in self.tasks]
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
        metadata: List[Tuple[str, dict]] = []

        def place(rel: str, info: dict) -> None:
            target = self._handle_file(local, rel, info)
            if target:
                produced.add(str(target))
                rows.append((info["id"], target.relative_to(local).as_posix(), False))

        for rel, info in entries:
            self.workers.check()  # 停下要直接丟出去：照常做完的話，沒列到的檔案會被當成已經刪掉
            if info["is_dir"]:
                if safe_rel(rel):
                    rows.append((info["id"], rel, True))
                continue
            newest = max(newest, info.get("mtime") or 0)
            if Path(rel).suffix.lower() in VIDEO_EXTS:
                place(rel, info)
            else:
                metadata.append((rel, info))
        # strm 先全部產生（不用問 115），中繼資料最後再一個一個下載（每個要取一次直鏈、要隔開）：
        # 新片不必等前面幾千個 nfo、海報下載完才出現
        for rel, info in metadata:
            self.workers.check()
            place(rel, info)
        # 刪舊 strm、換索引、存進度之前再看一次：最後一項做完才按停止，也照樣不刪、不存（下次同步重做只會是「未變」）
        self.workers.check()
        index = _TaskIndex(self.p115.db, task_key(task))
        if keep:
            # 目錄樹裡有、115 卻沒列出來的影片：strm 和索引都照舊保留
            known = index.files()
            rows += [(known[p], p, False) for p in keep if p in known]
        if self.cfg.delete_stale:
            if not complete:
                # 有檔案不知道放哪，當成不存在會誤刪
                self.result.notes.append(f"{remote}：有資料夾查不到路徑，這次不刪除本機多出來的 strm")
            elif not produced and not keep and _has_video(local):
                # 多半是 115 目錄填錯（另一個空資料夾）或 115 沒回完整，不能把整個媒體庫清掉
                self.result.notes.append(f"{remote}：115 上一支影片都沒列出來，這次不刪除本機的 strm")
            else:
                kept = produced | {str(local / p) for p in keep}
                self._remove_stale(local, kept | self._unknown_stale(remote, local, kept, index))
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
        if listed < in_tree * MIN_LISTED_RATIO:
            log.warning("115 列出的影片（%s）比目錄樹（%s）少很多，改成逐層列目錄", listed, in_tree)
            self.result.notes.append(f"{task.remote}：115 列出的影片比目錄樹少，改成逐層列目錄")
            return None
        hints = _TaskIndex(self.p115.db, task_key(task)).dirs()
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
        root = remote_root(task)
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
        below = _known_below(by_path)
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
                self.workers.check()  # 存讀到哪一筆之前：按了停止就不存，下次重讀（重做只會是「未變」）
                self._save_state(task, life_id=mine[-1]["id"], life_time=mine[-1]["mtime"])
        elif not pointer and self.p115.cookies:
            # 之前沒讀過事件（例如只用開放平台）：從現在開始記
            life = self._latest_event()
            if life[0]:
                self._save_state(task, life_id=life[0], life_time=life[1])
        self._scan_recent(ctx, state)
        for folder in sorted(ctx.to_prune, key=lambda f: len(f.parts), reverse=True):
            self._prune(ctx, folder)

    # ---------------- 增量：生活事件 ----------------

    def _apply_events(self, ctx: _Ctx, events: List[dict]) -> None:
        """同一個檔案只看最後一個事件，那就是它現在的狀態；依事件發生順序處理。"""
        log.info("處理 %s 個 115 生活事件（%s）", len(events), ctx.root)
        latest = sorted({e["file_id"]: e for e in events}.values(), key=lambda e: e["id"])
        for ev in latest:
            self.workers.check()
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
        if self.cfg.request_delay and self.workers.wait(self.cfg.request_delay):
            raise Stopped()
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
                self.workers.check()
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
            self.workers.check()
            newest = max(newest, info["mtime"])
            parent = self._remote_dir(info["parent_id"])
            rel = _rel(ctx.root, posixpath.join(parent, info["name"])) if parent is not None else None
            if rel:
                self._place_file(ctx, rel, info)
        self.workers.check()
        self._save_state(ctx.task, since=newest, incremental_at=int(time.time()))

    # ---------------- 本機檔案 ----------------

    def _target(self, rel: str, info: dict) -> Optional[Path]:
        """115 上的檔案在本機對應的相對路徑；不需要的檔案回傳 None。"""
        if not safe_rel(rel):
            log.warning("115 上的路徑會跑出本機的同步資料夾，略過：%r", rel)
            self.result.errors.append(f"{rel}: 路徑裡有「..」這類名稱，會寫到同步資料夾外面，略過")
            return None
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
            if not self._write_strm(local / target, info):
                return None
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
            self._move_sidecars(src, dst)
        shutil.move(str(src), str(dst))
        self._repath(src, dst, is_dir=False)
        self.result.moved += 1
        self.result.changed += [str(src), str(dst)]
        log.info("115 上移動或改名，本機跟著搬：%s -> %s", old, new)
        ctx.to_prune.add(src.parent)

    def _repath(self, src: Path, dst: Path, is_dir: bool) -> None:
        """本機的 strm（或整個資料夾）搬了：媒體庫裡的項目和媒體資訊跟著改路徑，項目的 id 不變。
        媒體庫的項目是用路徑認的，不改的話掃描會把舊路徑的項目刪掉、新路徑當成新項目，看過、續播點、收藏、
        片頭片尾紀錄全部不見（「整理 115 網盤」之後整部劇變成沒看過）。新路徑已經有項目的不動（OR IGNORE）。"""
        db, old, new = self.p115.db, str(src), str(dst)
        for table in ("items", "media_info"):
            if not is_dir:
                db.execute(f"UPDATE OR IGNORE {table} SET path=? WHERE path=?", (new, old))
                continue
            # 資料夾：它自己（劇）、底下的檔案和資料夾（old/…）、劇集沒有季資料夾時的「資料夾#season1」
            n = len(old) + 1
            db.execute(
                f"UPDATE OR IGNORE {table} SET path=? || substr(path, ?) "
                "WHERE path=? OR substr(path, 1, ?)=? OR substr(path, 1, ?)=?",
                (new, n, old, n, old + os.sep, n, old + "#"),
            )

    def _move_sidecars(self, src: Path, dst: Path) -> None:
        """strm 改名或搬家時，X.nfo、X-poster.jpg 這些跟著改成新名字（已有同名檔的不動）。"""
        for f in _sidecars(src.parent, src.stem):
            moved = dst.parent / (dst.stem + f.name[len(src.stem):])
            if not moved.exists():
                shutil.move(str(f), str(moved))

    def _move_dir(self, ctx: _Ctx, old: str, new: str) -> None:
        src, dst = ctx.local / old, ctx.local / new
        if src.is_dir():
            if dst.exists():
                self._merge_dir(src, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
            self._repath(src, dst, is_dir=True)
            self.result.moved += 1
            self.result.changed += [str(src), str(dst)]
            log.info("115 上移動或改名資料夾，本機跟著搬：%s -> %s", old, new)
            ctx.to_prune.add(src.parent)
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
            self.result.removed += _clear_tree(path)[0]
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
        ctx.to_prune.add(path.parent)

    def _prune(self, ctx: _Ctx, folder: Path) -> None:
        """由下往上清掉空資料夾；開了 delete_stale 時，已經沒有影片的資料夾裡的中繼資料也一起刪。

        只在一輪事件全部處理完後呼叫（ctx.to_prune），不會在「刪舊集、上傳新集」中間誤清。
        """
        while folder != ctx.local and ctx.local in folder.parents:
            if folder.is_dir():
                if self.cfg.delete_stale and not _has_video(folder):
                    self.result.changed.append(str(folder))
                    for f in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                        if f.is_file() and _is_metadata(f):
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

    def _write_strm(self, target: Path, info: dict) -> bool:
        """寫 strm；內容沒變就不動。回傳有沒有處理（115 沒給 pickcode 時略過）。"""
        if not info["pickcode"]:
            self.result.errors.append(f"{target.name}: 沒有 pickcode")
            log.warning("115 沒有回傳 pickcode，略過：%s", target)
            return False
        content = strm_content(self.cfg, info["pickcode"].lower(), info["name"], self.base_url)
        existed = target.is_file()
        old: Optional[str] = None
        if existed:
            try:
                old = target.read_text(encoding="utf-8").strip()
            except OSError:
                pass  # 讀不到就重寫；不知道舊內容，媒體資訊先留著
            if old == content:
                self.result.strm_unchanged += 1
                return True
        if old is not None and extract_pickcode(old) != info["pickcode"].lower():
            self._forget_media_info(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.result.strm_created += 1
        self.result.changed.append(str(target))
        if not existed:
            self.result.new_files.append(str(target))
        return True

    def _forget_media_info(self, target: Path) -> None:
        """115 上的檔案被換掉了（pickcode 變了）：舊的媒體資訊是另一個檔案的，刪掉等重新探測。

        只改伺服器網址、檔名附註而重寫 strm 時 pickcode 不變，媒體資訊照用。
        """
        sidecar = mediainfo_sidecar(target)
        had = sidecar.exists()
        sidecar.unlink(missing_ok=True)
        self.p115.db.execute("DELETE FROM media_info WHERE path=?", (str(target),))
        self.result.replaced.append(str(target))
        if had:
            log.info("115 上的檔案換了，舊的媒體資訊作廢：%s", target.name)

    def _download(self, target: Path, info: dict) -> None:
        if target.is_file() and target.stat().st_size == info["size"]:
            return
        if (info.get("size") or 0) > METADATA_MAX_BYTES:
            self.result.errors.append(f"{target.name}: 有 {info['size'] >> 20} MB，超過 {METADATA_MAX_BYTES >> 20} MB，不下載")
            return
        if not self._download_turn():
            return
        tmp = target.with_name(target.name + ".part")
        try:
            # 115 的 CDN 對 115Browser 的 UA 要 cookie，用一般瀏覽器的 UA
            url = self.p115.download_url(info["pickcode"], PLAIN_UA)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._fetch_to(url, tmp)
        except P115Throttled as exc:
            tmp.unlink(missing_ok=True)
            self._skip_metadata(str(exc))  # 熔斷已經設了，後面的不會再去問 115
            return
        except (P115Error, httpx.HTTPError) as exc:
            tmp.unlink(missing_ok=True)
            self.result.errors.append(f"{target.name}: {exc}")
            log.warning("下載 %s 失敗：%s", target.name, exc)
            return
        except BaseException:
            tmp.unlink(missing_ok=True)  # 磁碟滿、按了停止：不留寫一半的檔
            raise
        os.replace(tmp, target)
        self.result.metadata_downloaded += 1
        self.result.changed.append(str(target))

    def _download_turn(self) -> bool:
        """下載一個中繼資料之前：115 熔斷中就不下載（回傳 False，strm 照常產生）；兩次取直鏈之間至少隔
        metadata_pace 秒（request_delay 比較大就照它，熔斷剛恢復時再放慢）。上千個 nfo、海報一口氣取直鏈會被 115 限流。"""
        if self.p115.breaker.tripped:
            self._skip_metadata(self.p115.breaker.message())
            return False
        delay = self.cfg.request_delay or 0
        delay = max(delay, self.metadata_pace) * self.p115.breaker.slowdown() if delay else 0
        left = delay - (time.monotonic() - self._last_fetch)
        if left > 0 and self.workers.wait(left):
            raise Stopped()
        self._last_fetch = time.monotonic()
        return True

    def _skip_metadata(self, why: str) -> None:
        if not self.result.metadata_skipped:
            log.warning("115 限流，這次同步剩下的中繼資料先不下載：%s", why)
            self.result.notes.append(f"115 限流（{why}）：nfo、圖片、字幕這次先不下載，strm 照常產生；下次全量同步再補")
        self.result.metadata_skipped += 1

    def _fetch_to(self, url: str, tmp: Path) -> None:
        """把直鏈的內容一段一段寫進暫存檔（不整個讀進記憶體），超過 METADATA_MAX_BYTES 就放棄；CDN 回 405／429 算限流。"""
        with self._http.stream("GET", url, headers=self.p115.file_headers(url, PLAIN_UA)) as resp:
            if resp.status_code in (405, 429):
                self.p115.breaker.inspect(status=resp.status_code)
                raise P115Throttled(self.p115.breaker.message())
            resp.raise_for_status()
            size = 0
            with open(tmp, "wb") as f:
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > METADATA_MAX_BYTES:
                        raise P115Error(f"內容超過 {METADATA_MAX_BYTES >> 20} MB，不像中繼資料，不下載")
                    f.write(chunk)

    def _unknown_stale(self, remote: str, local: Path, kept: Set[str], index: "_TaskIndex") -> Set[str]:
        """這次要刪的 strm 裡，同步紀錄沒記過的（不是從這個 115 目錄同步來的）一大批時，這些先不刪，回傳它們。

        115 上刪掉、移走的檔案，同步紀錄裡都有，照刪。紀錄裡沒有的一大批，多半是 115 目錄改填成了另一個也有影片的
        資料夾（本機資料夾沒換），照刪會把本機媒體庫清掉一大半；只差幾個的照刪。
        """
        known = set(index.files())
        total, unknown = 0, set()
        for dirpath, _, filenames in os.walk(local):
            for name in filenames:
                if not name.lower().endswith(".strm"):
                    continue
                total += 1
                path = Path(dirpath) / name
                if str(path) not in kept and path.relative_to(local).as_posix() not in known:
                    unknown.add(str(path))
        if len(unknown) <= STALE_GUARD or len(unknown) <= total * STALE_GUARD_RATIO:
            return set()
        log.warning("%s：本機 %s 個 strm 裡有 %s 個 115 上沒有、同步紀錄裡也沒有，不刪", remote, total, len(unknown))
        self.result.notes.append(
            f"{remote}：本機有 {len(unknown)} 個 strm（共 {total} 個）在 115 上找不到、同步紀錄裡也沒有，不像是 115 上刪掉的，"
            "多半是同步任務的 115 目錄填錯了，這次不刪。確認沒填錯的話，自己刪掉本機這些 strm")
        return unknown

    def _remove_stale(self, local: Path, produced: set[str]) -> None:
        """刪除 115 上已不存在的 strm，以及跟著它的中繼資料。

        MoviePilot 刮削產生的 nfo／圖片不在 115 上，所以中繼資料不能只看「這次有沒有從 115 下載」：
        只刪掉與被刪 strm 同名的檔案（例如 X.nfo、X-poster.jpg、X.zh.srt），
        以及底下已經沒有任何影片的資料夾裡的中繼資料。其他副檔名的檔案一律不動。
        """
        gone_stems = self._remove_stale_strm(local, produced)
        for folder, stems in gone_stems.items():
            for stem in stems:
                for f in _sidecars(folder, stem):
                    if str(f) not in produced and f.exists():
                        f.unlink()
                        self.result.removed += 1
        self._remove_orphan_metadata(local, produced)

    def _remove_stale_strm(self, local: Path, produced: set[str]) -> Dict[Path, List[str]]:
        """刪掉這次沒產生的 strm；回傳每個資料夾刪掉了哪些檔名（不含副檔名）。"""
        by_lower = {p.lower(): p for p in produced}
        gone_stems: Dict[Path, List[str]] = {}
        for dirpath, _, filenames in os.walk(local):
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix.lower() != ".strm" or str(path) in produced:
                    continue
                twin = by_lower.get(str(path).lower())
                if twin and _same_file(path, Path(twin)):
                    continue  # 115 上只改了大小寫；macOS 這類大小寫不分的磁碟上還是同一個檔案
                if twin:
                    self._move_sidecars(path, Path(twin))  # 大小寫有分的磁碟：新舊是兩個檔，刮削資料跟著新名字
                path.unlink(missing_ok=True)
                self.result.removed += 1
                self.result.changed.append(str(path))
                gone_stems.setdefault(path.parent, []).append(path.stem)
        return gone_stems

    def _remove_orphan_metadata(self, local: Path, produced: set[str]) -> None:
        """由下往上：底下已經沒有影片的資料夾，刪掉裡面的中繼資料，空了就刪資料夾。"""
        for dirpath, dirnames, filenames in os.walk(local, topdown=False):
            folder = Path(dirpath)
            if folder == local:
                continue
            if _has_video(folder):
                continue
            for name in filenames:
                f = folder / name
                if _is_metadata(f) and str(f) not in produced:
                    f.unlink(missing_ok=True)
                    self.result.removed += 1
                    self.result.changed.append(str(f))
            try:
                folder.rmdir()  # 只有空資料夾才刪得掉
            except OSError:
                pass
