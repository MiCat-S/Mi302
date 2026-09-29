"""整理 115 網盤：媒體庫裡命名不照 MoviePilot 格式、或集號不對的，整個資料夾交給 MoviePilot 整理；不要的直接刪。

Mi302 自己不判斷名稱對不對，也不猜 TMDB 編號、類型和季，全部問 MoviePilot：
- 檢查：每個劇集、電影資料夾問 MoviePilot「整理後叫什麼」（GET /api/v1/transfer/name；資料夾問一次，再挑一兩支影片
  問檔名），它用自己的辨識和重命名格式回答。和現在的名稱不一樣就列出來，附上 MoviePilot 給的名稱；旁邊已經有那個
  名稱的資料夾，就是會併進去。MoviePilot 的劇集格式有季資料夾、影片卻直接放在劇集資料夾裡的，也列出來。
  問過的記在資料庫（organize_checks），資料夾名稱和裡面的影片沒變就不再問。幾千個資料夾第一次要問一陣子，在背景跑。
- 集號不對的劇：媒體庫掃描時集號是從檔名猜的、或認不出來的（items.ep_from），不用問 MoviePilot 也列出來。
- 瀏覽 115 裡挑的任何一個資料夾（folder_unit）：釘在清單最上面，一樣整理或刪除；不在同步目錄裡的（例如「待整理」）
  預設整理到 MoviePilot 目錄設定的媒體庫。
- 預覽：整個資料夾交給 MoviePilot（有子資料夾的一個子資料夾一次，直接放著的影片一次），和它網頁「檔案管理 → 整理」
  一樣，影片、字幕、音軌一起整理。預設什麼都不指定，讓它自己認；它認錯的（例如名稱裡的「预计第二季度」會被認成
  第 2 季）再在那一部分指定類型、TMDB 編號、季或集數定位（可以請 MoviePilot 推薦）。
  MoviePilot 沒有擋「新位置和原本一樣」，也不知道同步目錄在哪，所以 Mi302 自己擋：已經照格式命名的檔案、會把同步目錄裡
  的檔案搬出同步目錄的，那幾個檔案不送；一個資料夾裡有不送的檔案時，改成只送其他檔案（一個一個送）。
執行、清掉搬空的舊資料夾、之後的增量同步、刪除沿用 reorganize.Reorganizer。
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .db import Database
from .filetypes import VIDEO_EXTS
from .moviepilot import MoviePilot, MoviePilotError
from .p115 import P115Error
from .reorganize import episode_template
from .strm_sync import remote_root, task_key

log = logging.getLogger(__name__)

UNITS_TTL = 300  # 媒體庫裡有哪些資料夾，幾秒內直接用
LEVELS_META_KEY = "organize_levels"  # 上次檢查時 MoviePilot 的劇集、電影重命名格式各有幾層
PREVIEW_TIMEOUT = 600
SAMPLES = 2  # 一部劇挑幾支影片問 MoviePilot 檔名
MAX_WORKERS = 4  # 同時問 MoviePilot 幾個
MAX_PINNED = 20  # 從瀏覽 115 加進來的資料夾最多留幾個
RECOMMEND_FILES = 50  # 請 MoviePilot 推薦集數定位時最多給幾個檔名


class OrganizeError(Exception):
    pass


def _levels(template: str) -> int:
    """重命名格式有幾層（劇集預設三層：劇名資料夾／季資料夾／檔名；電影兩層）。"""
    return template.count("/") + 1 if template else 0


def _stem(name: str) -> str:
    return posixpath.splitext(name)[0]


def _is_video(name: str) -> bool:
    return posixpath.splitext(name)[1].lower() in VIDEO_EXTS


# ---------------- 媒體庫裡的資料夾 ----------------


@dataclass
class Part:
    """一個資料夾要分幾次送：整個資料夾、每個子資料夾、直接放在資料夾裡的影片。"""

    key: str
    label: str
    cid: int  # 送整個資料夾時是它的 id；loose 時是放影片的那個資料夾
    remote: str  # 115 上的完整路徑
    local: Optional[str]  # 本機對應的資料夾（不在同步目錄裡是 None）
    videos: Optional[int]  # 不知道（瀏覽 115 加進來的子資料夾）是 None
    lib_season: Optional[int] = None  # 媒體庫裡這些集大多是第幾季（只拿來提醒，不送給 MoviePilot）
    loose: bool = False  # 只送直接放在這個資料夾裡的影片（旁邊還有子資料夾）
    stems: List[str] = field(default_factory=list)


@dataclass
class Unit:
    id: str  # d{資料夾 id}；沒有自己資料夾的電影是 f{檔案 id}
    kind: str  # series / movie / movie_file / folder（瀏覽 115 加進來的）
    path: str  # 115 上的完整路徑（movie_file 是影片，不含副檔名）
    cid: int  # 資料夾 id；movie_file 是它所在的資料夾
    parent_cid: int
    name: str
    item_id: int  # 媒體庫裡的劇或電影；瀏覽 115 加進來的是 0
    title: str
    year: Optional[int]
    videos: Optional[int]
    loose: int  # 直接放在這個資料夾裡的影片數
    samples: List[str]  # 問 MoviePilot 檔名用的影片（115 路徑）
    siblings: List[str]  # 同一層的其他資料夾
    in_sync: bool = True  # 在同步目錄裡（整理後不能搬出同步目錄）
    local: Optional[str] = None  # 本機對應的位置
    file_id: int = 0  # movie_file：影片的 id
    parts: List[Part] = field(default_factory=list)
    ep_guessed: int = 0  # 媒體庫裡集號是從檔名猜的集數
    ep_unknown: int = 0  # 認不出集號的集數
    pinned: bool = False
    # 問過 MoviePilot 之後
    checked: bool = False
    mp_name: str = ""
    mp_files: List[List[str]] = field(default_factory=list)
    error: str = ""
    reasons: List[str] = field(default_factory=list)
    merge_into: Optional[dict] = None

    @property
    def parent(self) -> str:
        return posixpath.dirname(self.path)

    @property
    def sig(self) -> str:
        return hashlib.sha1(json.dumps([self.name, self.samples, self.videos, self.loose]).encode()).hexdigest()[:16]

    @property
    def listed(self) -> bool:
        """要不要出現在清單上：問過 MoviePilot、和它的不一樣，或集號不對，或是瀏覽 115 加進來的。"""
        return self.pinned or bool(self.ep_guessed or self.ep_unknown) or (self.checked and bool(self.reasons))

    def view(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "name": self.name, "path": self.path, "parent": self.parent,
            "item_id": self.item_id, "title": self.title, "year": self.year, "videos": self.videos, "in_sync": self.in_sync,
            "type": "movie" if self.kind in ("movie", "movie_file") else "tv" if self.kind == "series" else "",
            "reasons": self.reasons, "error": self.error, "checked": self.checked, "pinned": self.pinned,
            "mp_name": self.mp_name, "mp_files": self.mp_files, "merge_into": self.merge_into,
            "ep_guessed": self.ep_guessed, "ep_unknown": self.ep_unknown,
            "parts": [{"key": p.key, "label": p.label, "videos": p.videos, "lib_season": p.lib_season} for p in self.parts],
        }


class _Tree:
    """一個同步任務在 115 同步紀錄裡的目錄樹（本機的相對路徑）。"""

    def __init__(self, db: Database, task):
        key = task_key(task)
        self.dirs: Dict[str, int] = {r["path"]: r["file_id"] for r in
                                     db.query("SELECT file_id, path FROM p115_index WHERE task=? AND is_dir=1", (key,))}
        self.children: Dict[str, List[str]] = {}
        for d in self.dirs:
            self.children.setdefault(posixpath.dirname(d), []).append(posixpath.basename(d))
        self.strm: Dict[str, List[Tuple[int, str]]] = {}  # 資料夾 → [(檔案 id, strm 檔名)]
        for r in db.query("SELECT file_id, path FROM p115_index WHERE task=? AND is_dir=0", (key,)):
            ext = posixpath.splitext(r["path"])[1].lower()
            if ext == ".strm" or ext in VIDEO_EXTS:
                self.strm.setdefault(posixpath.dirname(r["path"]), []).append((r["file_id"], posixpath.basename(r["path"])))
        self.total: Dict[str, int] = Counter()  # 資料夾（含子資料夾）裡有幾支影片
        for folder, videos in self.strm.items():
            d = folder
            while True:
                self.total[d] += len(videos)
                if not d:
                    break
                d = posixpath.dirname(d)

    def walk(self, rel: str) -> List[str]:
        out, stack = [], [rel]
        while stack:
            d = stack.pop()
            out.append(d)
            stack += [posixpath.join(d, c) if d else c for c in self.children.get(d, [])]
        return out

    def videos_under(self, rel: str) -> List[str]:
        """資料夾（含子資料夾）裡的影片，相對路徑，照名稱排。"""
        return sorted(posixpath.join(d, n) if d else n for d in self.walk(rel) for _, n in self.strm.get(d, []))


def _samples(videos: List[str], root: str) -> List[str]:
    picks = [videos[0], videos[-1]] if len(videos) > 1 else videos[:1]
    return [posixpath.join(root, v) for v in dict.fromkeys(picks)][:SAMPLES]


def _season_mode(seasons) -> Optional[int]:
    counts = Counter(s for s in seasons if s is not None and s >= 0)
    return counts.most_common(1)[0][0] if counts else None


def find_units(db: Database, tasks) -> List[Unit]:
    """媒體庫裡在同步目錄底下的劇集資料夾、電影資料夾、沒有自己資料夾的電影（照 115 路徑排）。只看結構和媒體庫。"""
    units: List[Unit] = []
    for task in tasks:
        local = str(Path(task.local).expanduser())
        root = remote_root(task)
        tree = _Tree(db, task)
        lo, hi = local + "/", local + "/\U0010ffff"
        eps: Dict[str, List[Tuple[Optional[int], str]]] = {}  # 本機資料夾 → [(季, ep_from)]
        for r in db.query("SELECT path, parent_index_number, ep_from FROM items WHERE type='Episode' AND is_strm=1 "
                          "AND path>=? AND path<?", (lo, hi)):
            eps.setdefault(posixpath.dirname(r["path"]), []).append((r["parent_index_number"], r["ep_from"] or ""))
        for s in db.query("SELECT id, name, year, path FROM items WHERE type='Series' AND path>=? AND path<?", (lo, hi)):
            rel = s["path"][len(lo):]
            if rel in tree.dirs and tree.total.get(rel):
                units.append(_series_unit(task, tree, s, rel, root, local, eps))
        for m in db.query("SELECT id, name, year, path FROM items WHERE type='Movie' AND is_strm=1 AND path>=? AND path<?", (lo, hi)):
            unit = _movie_unit(tree, m, m["path"][len(lo):], root, local)
            if unit:
                units.append(unit)
    units.sort(key=lambda u: u.path)
    return units


def _series_unit(task, tree: _Tree, s, rel: str, root: str, local: str, eps) -> Unit:
    cid, parent = tree.dirs[rel], posixpath.dirname(rel)
    loose = tree.strm.get(rel, [])
    subdirs = sorted(c for c in tree.children.get(rel, []) if tree.total.get(posixpath.join(rel, c)))
    here = [e for d in tree.walk(rel) for e in eps.get(str(Path(local) / d), [])]

    def lib_season(folder_rel: str, only_here: bool) -> Optional[int]:
        folders = [folder_rel] if only_here else tree.walk(folder_rel)
        return _season_mode(sn for f in folders for sn, _ in eps.get(str(Path(local) / f), []))

    def part(key, label, part_cid, part_rel, videos, season, **kw) -> Part:
        return Part(key, label, part_cid, posixpath.join(root, part_rel), str(Path(local) / part_rel), videos, season, **kw)

    parts: List[Part] = []
    if not subdirs:
        parts.append(part("all", "整個資料夾", cid, rel, len(loose), lib_season(rel, True)))
    else:
        for c in subdirs:
            c_rel = posixpath.join(rel, c)
            parts.append(part(f"d{tree.dirs[c_rel]}", c, tree.dirs[c_rel], c_rel, tree.total[c_rel], lib_season(c_rel, False)))
        if loose:
            parts.append(part("loose", "直接放在資料夾裡的影片", cid, rel, len(loose), lib_season(rel, True),
                              loose=True, stems=[_stem(n) for _, n in loose]))
    return Unit(f"d{cid}", "series", posixpath.join(root, rel), cid, tree.dirs.get(parent, 0), posixpath.basename(rel),
                s["id"], s["name"], s["year"], tree.total[rel], len(loose), _samples(tree.videos_under(rel), root),
                [c for c in tree.children.get(parent, []) if c != posixpath.basename(rel)], local=str(Path(local) / rel),
                parts=parts, ep_guessed=sum(1 for _, f in here if f == "name"), ep_unknown=sum(1 for _, f in here if f == "none"))


def _movie_unit(tree: _Tree, m, strm_rel: str, root: str, local: str) -> Optional[Unit]:
    folder, strm_name = posixpath.dirname(strm_rel), posixpath.basename(strm_rel)
    here = tree.strm.get(folder, [])
    fid = next((f for f, n in here if n == strm_name), None)
    if fid is None:
        return None
    # 自己一個資料夾：資料夾裡只有這一支影片，也沒有放影片的子資料夾（那是分類資料夾）
    own = bool(folder) and len(here) == 1 and folder in tree.dirs and not any(
        tree.total.get(posixpath.join(folder, c)) for c in tree.children.get(folder, []))
    if own:
        cid, parent = tree.dirs[folder], posixpath.dirname(folder)
        return Unit(f"d{cid}", "movie", posixpath.join(root, folder), cid, tree.dirs.get(parent, 0), posixpath.basename(folder),
                    m["id"], m["name"], m["year"], 1, 1, [posixpath.join(root, strm_rel)],
                    [c for c in tree.children.get(parent, []) if c != posixpath.basename(folder)], local=str(Path(local) / folder),
                    parts=[Part("all", "整個資料夾", cid, posixpath.join(root, folder), str(Path(local) / folder), 1)])
    cid = tree.dirs.get(folder, 0)
    return Unit(f"f{fid}", "movie_file", posixpath.join(root, folder, _stem(strm_name)), cid,
                tree.dirs.get(posixpath.dirname(folder), 0) if folder else 0, _stem(strm_name), m["id"], m["name"], m["year"],
                1, 1, [posixpath.join(root, strm_rel)], tree.children.get(folder, []), local=str(Path(local) / strm_rel),
                file_id=fid, parts=[Part("file", "這支影片", cid, posixpath.join(root, folder), str(Path(local) / folder), 1,
                                         loose=True, stems=[_stem(strm_name)])])


def judge(unit: Unit, tv_levels: int, movie_levels: int) -> None:
    """依 MoviePilot 的回答（和媒體庫的集號）決定要不要整理，原因寫進 unit.reasons。"""
    unit.reasons, unit.merge_into = [], None
    if unit.ep_guessed or unit.ep_unknown:
        unit.reasons.append("媒體庫裡 " + "、".join(t for t in (f"{unit.ep_guessed} 集的集號是從檔名猜的" if unit.ep_guessed else "",
                                                              f"{unit.ep_unknown} 集認不出集號" if unit.ep_unknown else "") if t))
    if not unit.checked:
        return
    if unit.error:
        unit.reasons.append(f"MoviePilot 認不出來：{unit.error}")
        return
    if unit.kind == "movie_file":
        if movie_levels >= 2:
            unit.reasons.append("沒有自己的資料夾（MoviePilot 會放進自己的資料夾）")
    elif unit.mp_name and unit.mp_name != unit.name:
        unit.reasons.append("資料夾名稱和 MoviePilot 的不一樣")
        if unit.mp_name in unit.siblings:
            unit.merge_into = {"name": unit.mp_name, "path": posixpath.join(unit.parent, unit.mp_name)}
    if unit.kind == "series" and tv_levels >= 3 and unit.loose:
        unit.reasons.append(f"{unit.loose} 支影片直接放在資料夾裡（MoviePilot 會放進季資料夾）")
    if any(_stem(f[0]) != _stem(f[1]) for f in unit.mp_files):
        unit.reasons.append("檔名和 MoviePilot 的不一樣")


# ---------------- 檢查（在背景問 MoviePilot） ----------------


@dataclass
class CheckJob:
    running: bool = False
    started: float = 0.0
    finished: float = 0.0
    total: int = 0  # 媒體庫裡的資料夾數
    todo: int = 0  # 這次要問的
    done: int = 0  # 這次問完的
    found: int = 0  # 目前要整理的
    current: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class Organizer:
    def __init__(self, db: Database, strm_sync, moviepilot: MoviePilot, reorganizer, scanner=None):
        self.db = db
        self.strm_sync = strm_sync
        self.mp = moviepilot
        self.reorg = reorganizer
        self.scanner = scanner
        self._stamp: tuple = ()  # 上次算清單時媒體庫掃描、115 同步完成的時間；變了就重算
        self.job = CheckJob()
        self._lock = threading.Lock()  # 同時只跑一個檢查
        self._units: Dict[str, Unit] = {}
        self._order: List[str] = []
        self._units_at = 0.0
        self._pinned: Dict[str, Unit] = {}  # 從瀏覽 115 加進來的資料夾（新的在前面）
        try:
            tv, movie = json.loads(db.get_meta(LEVELS_META_KEY) or "[3, 2]")
            self._levels: Tuple[int, int] = (int(tv), int(movie))
        except (ValueError, TypeError):
            self._levels = (3, 2)

    @property
    def p115(self):
        return self.strm_sync.p115

    def _roots(self) -> List[str]:
        return [remote_root(t) for t in self.strm_sync.tasks]

    def _read_levels(self) -> None:
        """問 MoviePilot 的重命名格式各有幾層（有沒有季資料夾、電影有沒有自己的資料夾）；讀不到就沿用上次的。"""
        try:
            tv, movie = self.mp.rename_formats()
        except MoviePilotError as exc:
            log.info("讀不到 MoviePilot 的重命名格式，沿用上次的：%s", exc)
            return
        self._levels = (_levels(tv) or 3, _levels(movie) or 2)
        self.db.set_meta(LEVELS_META_KEY, json.dumps(list(self._levels)))

    def units(self, refresh: bool = False) -> List[Unit]:
        """媒體庫裡的資料夾，套上資料庫裡問過 MoviePilot 的結果（資料夾名稱或影片變了的算沒問過）。"""
        stamp = (getattr(self.scanner, "finished_at", 0), self.strm_sync.result.finished)
        if refresh or stamp != self._stamp or not self._units_at or time.time() - self._units_at > UNITS_TTL:
            self._stamp = stamp
            units = find_units(self.db, self.strm_sync.tasks)
            self._units = {u.id: u for u in units}
            self._order = [u.id for u in units]
            self._units_at = time.time()
            self._apply_cache(units)
        return [self._units[i] for i in self._order]

    def _apply_cache(self, units: List[Unit]) -> None:
        cached = {r["path"]: r for r in self.db.query("SELECT * FROM organize_checks")}
        for u in units:
            r = cached.get(u.path)
            if r and r["sig"] == u.sig:
                u.checked, u.mp_name, u.error = True, r["name"] or "", r["error"] or ""
                u.mp_files = json.loads(r["files"] or "[]")
            else:
                u.checked = False
            judge(u, *self._levels)

    def unit(self, unit_id: str) -> Unit:
        unit = self._pinned.get(unit_id) or next((u for u in self.units() if u.id == unit_id), None)
        if not unit:
            raise OrganizeError("清單已經更新，找不到這個資料夾，請重新整理")
        return unit

    # ---- 問 MoviePilot ----

    def check_in_background(self, refresh: bool = False) -> bool:
        """在背景問 MoviePilot；refresh=True 時問過的也重問（例如改了 MoviePilot 的重命名格式）。"""
        if not self.mp.can_subscribe:
            raise OrganizeError("問 MoviePilot 要用帳號登入：請在「MoviePilot」頁填帳號密碼")
        if not self._lock.acquire(blocking=False):
            return False
        self.job = CheckJob(running=True, started=time.time())
        threading.Thread(target=self._check, args=(refresh,), daemon=True).start()
        return True

    def _check(self, refresh: bool) -> None:
        job = self.job
        try:
            self._read_levels()
            units = self.units(refresh=True)
            job.total = len(units)
            todo = [u for u in units if refresh or not u.checked]
            job.todo = len(todo)
            asking = {u.id for u in todo}
            job.found = sum(1 for u in units if u.listed and u.id not in asking)
            log.info("整理 115 網盤：媒體庫裡 %s 個資料夾，這次問 MoviePilot %s 個", len(units), len(todo))
            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, self.mp.concurrency)) as pool:
                futures = {pool.submit(self._ask, u): u for u in todo}
                for fut in as_completed(futures):
                    u = futures[fut]
                    try:
                        fut.result()
                    except MoviePilotError as exc:  # 連不上、被拒絕：後面的也不會成功，停下來
                        job.error = f"問 MoviePilot 時失敗：{exc}"
                        for f in futures:
                            f.cancel()
                        break
                    job.done += 1
                    job.current = u.name
                    job.found += 1 if u.listed else 0
            live = {u.path for u in units} | {u.path for u in self._pinned.values()}
            with self.db.lock:
                old = [r["path"] for r in self.db.query("SELECT path FROM organize_checks")]
                self.db.conn.executemany("DELETE FROM organize_checks WHERE path=?", [(p,) for p in old if p not in live])
                self.db.conn.commit()
            log.info("整理 115 網盤：問完 %s 個，%s 個要整理", job.done, job.found)
        except Exception as exc:  # 背景執行緒：記下來，不讓網頁一直顯示「檢查中」
            job.error = job.error or f"{type(exc).__name__}: {exc}"
            log.exception("整理 115 網盤檢查時發生錯誤")
        finally:
            job.current = ""
            job.running = False
            job.finished = time.time()
            self._lock.release()

    def _ask(self, u: Unit) -> None:
        """問 MoviePilot 這個資料夾和幾支影片整理後叫什麼，記進資料庫。認不出來的記下原因。"""
        u.mp_name, u.mp_files, u.error = "", [], ""
        if u.kind != "movie_file":
            ok, text = self.mp.transfer_name(u.path, "dir")
            if ok:
                u.mp_name = text
            else:
                u.error = text
        if not u.error:
            for sample in u.samples:
                ok, text = self.mp.transfer_name(sample, "file")
                if ok:
                    u.mp_files.append([posixpath.basename(sample), text])
                elif u.kind == "movie_file":
                    u.error = text
        u.checked = True
        judge(u, *self._levels)
        self.db.execute(
            "INSERT INTO organize_checks(path, sig, name, files, error, at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET sig=excluded.sig, name=excluded.name, files=excluded.files, "
            "error=excluded.error, at=excluded.at",
            (u.path, u.sig, u.mp_name, json.dumps(u.mp_files, ensure_ascii=False), u.error, int(time.time())))

    # ---- 瀏覽 115 裡挑的資料夾 ----

    def folder_unit(self, cid: int, path: str) -> dict:
        """瀏覽 115 裡的一個資料夾：列一次目錄分成幾部分、問 MoviePilot 叫什麼，釘在清單最上面。"""
        path = "/" + path.strip("/") if path.strip("/") else "/"
        if not cid or path == "/":
            raise OrganizeError("不能整理整個 115，請選一個資料夾")
        try:
            entries = self.p115.list_dir(cid)
            ancestors = self.p115.dir_ancestors(cid)  # 由根往下到這個資料夾；倒數第二個是上一層
            parent_cid = ancestors[-2][0] if len(ancestors) > 1 else 0
        except P115Error as exc:
            raise OrganizeError(f"讀不到 115：{exc}")
        dirs = sorted((e for e in entries if e["is_dir"]), key=lambda e: e["name"])
        videos = sorted((e for e in entries if not e["is_dir"] and _is_video(e["name"])), key=lambda e: e["name"])
        if not dirs and not videos:
            raise OrganizeError("這個資料夾裡沒有影片，也沒有子資料夾")
        roots = self._roots()
        in_sync = any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)
        local = self._local_of(path)
        if not dirs:
            parts = [Part("all", "整個資料夾", cid, path, local, len(videos))]
        else:
            parts = [Part(f"d{d['id']}", d["name"], d["id"], posixpath.join(path, d["name"]),
                          str(Path(local) / d["name"]) if local else None, None) for d in dirs]
            if videos:
                parts.append(Part("loose", "直接放在資料夾裡的影片", cid, path, local, len(videos), loose=True,
                                  stems=[_stem(v["name"]) for v in videos]))
        unit = Unit(f"d{cid}", "folder", path, cid, parent_cid, posixpath.basename(path), 0, "", None,
                    len(videos) if not dirs else None, len(videos), [posixpath.join(path, v["name"]) for v in videos[:1]],
                    [], in_sync=in_sync, local=local, parts=parts, pinned=True)
        if self.mp.can_subscribe:
            try:
                self._ask(unit)
            except MoviePilotError as exc:
                unit.error = f"問不到 MoviePilot：{exc}"
        self._pinned.pop(unit.id, None)
        self._pinned = {unit.id: unit, **self._pinned}
        for extra in list(self._pinned)[MAX_PINNED:]:
            self._pinned.pop(extra)
        return unit.view()

    def unpin(self, unit_id: str) -> None:
        self._pinned.pop(unit_id, None)

    def _local_of(self, path: str) -> Optional[str]:
        """115 路徑在本機對應的位置（不在任何同步任務裡是 None）。"""
        for t in self.strm_sync.tasks:
            root = remote_root(t)
            if path == root or path.startswith(root.rstrip("/") + "/"):
                return str(Path(t.local).expanduser() / path[len(root):].lstrip("/"))
        return None

    # ---- 清單 ----

    def list(self, q: str = "", kind: str = "", offset: int = 0, limit: int = 50) -> dict:
        units = self.units()
        found = [u for u in units if u.listed]
        counts = {"series": sum(1 for u in found if u.kind == "series"), "movie": sum(1 for u in found if u.kind != "series"),
                  "episodes": sum(1 for u in found if u.ep_guessed or u.ep_unknown)}
        text = q.strip().casefold()

        def wanted(u: Unit) -> bool:
            if kind == "series" and u.kind != "series" or kind == "movie" and u.kind == "series":
                return False
            if kind == "episodes" and not (u.ep_guessed or u.ep_unknown):
                return False
            return not text or text in u.name.casefold() or text in (u.title or "").casefold() or text in u.path.casefold()

        shown = [u for u in found if wanted(u) and u.id not in self._pinned]
        return {
            "job": self.job.as_dict(), "total": len(shown), "folders": len(units),
            "unchecked": sum(1 for u in units if not u.checked), "counts": counts,
            "pinned": [u.view() for u in self._pinned.values()],
            "items": [u.view() for u in shown[offset:offset + limit]],
        }

    def nonstandard_series(self) -> Set[int]:
        """要整理的劇（問過 MoviePilot 不一樣的，或集號不對的）。"""
        return {u.item_id for u in self.units() if u.kind == "series" and u.listed}

    # ---------------- 預覽 ----------------

    def preview(self, unit_id: str, overrides: Dict[str, dict], target: str = "", target_path: str = "",
                scrape: bool = True) -> dict:
        """請 MoviePilot 只算不做，一個部分一次。overrides：{部分: {type, tmdbid, season, format}}，沒給的讓它自己認。
        target：parent（同一層，同步目錄裡的預設）、auto（照 MoviePilot 的目錄設定，同步目錄外的預設）、path（target_path）。"""
        unit = self.unit(unit_id)
        try:
            self.mp.check_transfer_preview()  # 舊版 MoviePilot 會把預覽當成真的整理
        except MoviePilotError as exc:
            raise OrganizeError(str(exc))
        target = target or ("parent" if unit.in_sync else "auto")
        if target == "path":
            dest = "/" + str(target_path or "").strip().strip("/")
            if dest == "/":
                raise OrganizeError("請填要整理到哪個 115 資料夾")
        else:
            dest = unit.parent if target == "parent" else None
        roots = self._roots()
        items: List[dict] = []
        batches: List[dict] = []
        notes: List[str] = []
        parts: List[dict] = []
        listings: Dict[str, List[dict]] = {}
        for part in unit.parts:
            o = _override(overrides.get(part.key) or {})
            try:
                fileitems, single = self._fileitems(unit, part, listings)
            except P115Error as exc:
                raise OrganizeError(f"讀不到 115 上的檔案：{exc}")
            if not fileitems:
                notes.append(f"「{part.label}」在 115 上找不到影片，這次不送（先同步一次）")
                continue
            try:
                results = self.mp.transfer(fileitems, o["tmdbid"] or None, o["season"], o["format"] or None, scrape, dest,
                                           preview=True, mtype=o["type_name"], timeout=PREVIEW_TIMEOUT, single=single)
            except MoviePilotError as exc:
                raise OrganizeError(f"MoviePilot 預覽失敗：{exc}")
            views = [_view(r, part, roots) for r in results]
            _mark_duplicates(views)
            recognized = _recognized(views)
            if part.lib_season is not None and o["season"] is None:
                for r in recognized:
                    if r["season"] is not None and r["season"] != part.lib_season and r["type"] != "电影":
                        notes.append(f"「{part.label}」MoviePilot 認成第 {r['season']} 季，媒體庫裡是第 {part.lib_season} 季；"
                                     "不對的話在這一部分指定季，再預覽一次")
            skipped = [v for v in views if v["skip"]]
            if skipped:
                same = sum(1 for v in skipped if v["skip"] == "same")
                out = len(skipped) - same
                notes.append(f"「{part.label}」" + "、".join(t for t in (f"{same} 個已經照格式命名" if same else "",
                                                                         f"{out} 個會搬出同步目錄" if out else "") if t)
                             + "，這些不送")
                # 不送的檔案不能跟著送：改成只送其他影片（一個一個送），字幕跟著同名的影片走
                try:
                    fileitems = self._items_for(unit, part, [v["source"] for v in views if v["ok"] and _is_video(v["source"])],
                                                listings)
                except P115Error as exc:
                    raise OrganizeError(f"讀不到 115 上的檔案：{exc}")
                single = False
            items += views
            ok = sum(1 for v in views if v["ok"])
            parts.append({"key": part.key, "label": part.label, "recognized": recognized, "ok": ok,
                          "failed": sum(1 for v in views if not v["ok"] and not v["skip"])})
            if ok and fileitems:
                batches.append({"fileitems": fileitems, "single": single and len(fileitems) == 1, "season": o["season"],
                                "tmdbid": o["tmdbid"], "type_name": o["type_name"], "episode_format": o["format"],
                                "count": ok, "label": part.label, "local": part.local})
        folders = sorted({_top_folder(v["target"], dest) for v in items if v["ok"] and v["target"] and dest} - {""})
        token = self._remember(unit, batches, dest, scrape) if batches else None
        return {
            "token": token, "items": items, "notes": notes, "folders": folders, "parts": parts, "target": target,
            "summary": {"total": len(items), "ok": sum(1 for i in items if i["ok"]),
                        "failed": sum(1 for i in items if not i["ok"] and not i["skip"]),
                        "skipped": sum(1 for i in items if i["skip"]),
                        "warnings": sum(1 for i in items if i["ok"] and i["warnings"])},
        }

    def _fileitems(self, unit: Unit, part: Part, listings: Dict[str, List[dict]]) -> Tuple[List[dict], bool]:
        """這一部分要送給 MoviePilot 的項目：整個資料夾一個（single），直接放著的影片照 115 上的檔名一個一個。"""
        if not part.loose:
            name = posixpath.basename(part.remote)
            parent_cid = unit.parent_cid if part.cid == unit.cid else unit.cid
            return [{"storage": "u115", "type": "dir", "path": part.remote.rstrip("/") + "/", "name": name, "basename": name,
                     "fileid": str(part.cid), "parent_fileid": str(parent_cid)}], True
        wanted = set(part.stems)
        out = [self._file_item(part.remote, part.cid, e) for e in self._listing(part.remote, part.cid, listings)
               if not e["is_dir"] and _is_video(e["name"]) and _stem(e["name"]) in wanted]
        return out, len(out) == 1

    def _listing(self, path: str, cid: Optional[int], listings: Dict[str, List[dict]]) -> List[dict]:
        if path not in listings:
            listings[path] = self.p115.list_dir(cid if cid else self.p115.dir_id(path))
        return listings[path]

    @staticmethod
    def _file_item(folder: str, cid: int, e: dict) -> dict:
        stem, ext = posixpath.splitext(e["name"])
        return {"storage": "u115", "type": "file", "path": posixpath.join(folder, e["name"]), "name": e["name"],
                "basename": stem, "extension": ext.lstrip(".").lower(), "size": int(e.get("size") or 0),
                "fileid": str(e["id"]), "parent_fileid": str(cid), "pickcode": e.get("pickcode") or ""}

    def _items_for(self, unit: Unit, part: Part, sources: List[str], listings: Dict[str, List[dict]]) -> List[dict]:
        """MoviePilot 預覽回的這幾個 115 路徑，換成可以一個一個送的檔案項目（它搬 115 的檔案要 fileid）。"""
        known = {part.remote.rstrip("/"): part.cid, unit.path: unit.cid}
        out = []
        for folder in dict.fromkeys(posixpath.dirname(s) for s in sources):
            cid = known.get(folder) or self.p115.dir_id(folder)  # 更深的子資料夾才要查
            entries = self._listing(folder, cid, listings)
            names = {posixpath.basename(s) for s in sources if posixpath.dirname(s) == folder}
            out += [self._file_item(folder, cid, e) for e in entries if not e["is_dir"] and e["name"] in names]
        return out

    def _remember(self, unit: Unit, batches: List[dict], dest: Optional[str], scrape: bool) -> str:
        payload = {"plan_id": f"o{unit.id}", "mode": "organize", "title": unit.path,
                   "cid": unit.cid if unit.kind != "movie_file" else None, "target_path": dest, "scrape": bool(scrape),
                   "batches": batches}
        token = hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]
        self.reorg.remember_preview(token, payload)
        return token

    # ---- 集數定位 ----

    def recommend(self, unit_id: str, part_key: str) -> dict:
        """這一部分的集數定位模板：先請 MoviePilot 推薦，推薦不出來再用 Mi302 從檔名看的（標明是誰給的）。"""
        unit = self.unit(unit_id)
        part = next((p for p in unit.parts if p.key == part_key), None)
        if not part:
            raise OrganizeError("找不到這一部分，請重新整理")
        listings: Dict[str, List[dict]] = {}
        try:
            entries = self._listing(part.remote, part.cid, listings)
        except P115Error as exc:
            raise OrganizeError(f"讀不到 115：{exc}")
        wanted = set(part.stems) if part.loose else None
        videos = [e for e in entries if not e["is_dir"] and _is_video(e["name"]) and (wanted is None or _stem(e["name"]) in wanted)]
        if not videos:
            raise OrganizeError("這一部分直接放著的影片沒有，推薦不了（有子資料夾的話到子資料夾那一部分推薦）")
        fileitems = [self._file_item(part.remote, part.cid, e) for e in videos[:RECOMMEND_FILES]]
        why = ""
        if self.mp.can_subscribe:
            template, why = self.mp.recommend_format(fileitems)
            if template:
                return {"format": template, "source": "moviepilot", "note": why}
        for e in videos:
            template = episode_template(e["name"])
            if template:
                return {"format": template, "source": "mi302",
                        "note": f"MoviePilot 推薦不出來（{why or '沒有帳號登入'}），這是 Mi302 從「{e['name']}」看的"}
        return {"format": "", "source": "", "note": f"MoviePilot 推薦不出來（{why or '沒有帳號登入'}），Mi302 也看不出集號在哪"}

    # ---- 刪除（電影、瀏覽 115 加進來的資料夾；劇集用 Reorganizer.delete_episodes／delete_series）----

    def delete(self, unit_id: str) -> dict:
        unit = self.unit(unit_id)
        if unit.kind == "series":
            return self.reorg.delete_series(unit.item_id)
        is_dir = unit.kind != "movie_file"
        result = self.reorg.delete_item(unit.cid if is_dir else unit.file_id, is_dir, unit.path, unit.local)
        self._pinned.pop(unit.id, None)
        self._units_at = 0  # 清單重新算
        return result


def _override(o: dict) -> dict:
    """網頁上某一部分的指定：類型 tv／movie、TMDB 編號、季、集數定位；空的讓 MoviePilot 自己認。"""
    tmdbid = str(o.get("tmdbid") or "").strip()
    if tmdbid and not tmdbid.isdigit():
        raise OrganizeError("TMDB 編號要是數字")
    season = o.get("season")
    season = int(season) if str(season if season is not None else "").strip().isdigit() else None
    type_name = {"tv": "电视剧", "movie": "电影"}.get(str(o.get("type") or ""))
    fmt = str(o.get("format") or "").strip()
    if fmt and "{ep}" not in fmt:
        raise OrganizeError("集數定位要有 {ep}（集號的位置），例如 {ep}.{a}")
    if type_name == "电影":
        season, fmt = None, ""
    return {"tmdbid": tmdbid, "season": season, "type_name": type_name, "format": fmt}


def _view(r: dict, part: Part, roots: List[str]) -> dict:
    """MoviePilot 預覽的一個檔案，加上 Mi302 的檢查（MoviePilot 沒有擋來源等於目標，也不知道同步目錄在哪）。"""
    source = str(r.get("source") or "")
    target = str(r.get("target") or r.get("target_dir") or "")
    episode = int(r["episode"]) if str(r.get("episode") or "").isdigit() else None
    season = int(r["season"]) if str(r.get("season") if r.get("season") is not None else "").isdigit() else None
    ok = bool(r.get("success")) and bool(target)
    message, warnings, skip = str(r.get("message") or ""), [], ""

    def inside(p: str) -> bool:
        return any(p.startswith(root.rstrip("/") + "/") for root in roots)

    if ok and target.rstrip("/") == source.rstrip("/"):
        ok, skip, message = False, "same", "已經照格式命名，不送"
    elif ok and not inside(target):
        if inside(source):
            ok, skip, message = False, "outside", "新位置不在 Mi302 的 115 同步目錄裡，整理後會從媒體庫消失，不送"
        else:
            warnings.append("新位置不在 Mi302 的 115 同步目錄裡，Mi302 不會替它產生 strm")
    if not ok and not message:
        message = "MoviePilot 沒有說明原因"
    return {"name": posixpath.basename(source.rstrip("/")), "source": source, "target": target, "episode": episode,
            "season": season, "title": str(r.get("title") or ""), "type": str(r.get("type") or ""), "ok": ok,
            "skip": skip, "message": message, "warnings": warnings, "part": part.label}


def _recognized(views: List[dict]) -> List[dict]:
    """MoviePilot 把這一部分認成什麼：片名、類型、季，各幾個檔案。"""
    counts = Counter((v["title"], v["type"], v["season"]) for v in views if v["ok"])
    return [{"title": t, "type": ty, "season": s, "count": n} for (t, ty, s), n in counts.most_common()]


def _mark_duplicates(views: List[dict]) -> None:
    """兩個檔案整理到同一個位置：後整理的會被 MoviePilot 跳過，提醒一下。"""
    seen = Counter(v["target"] for v in views if v["ok"] and v["target"])
    for v in views:
        if v["ok"] and seen.get(v["target"], 0) > 1:
            v["warnings"].append(f"有 {seen[v['target']]} 個檔案會整理到同一個位置，只會留一個")


def _top_folder(target: str, parent: str) -> str:
    rest = target[len(parent.rstrip("/")) + 1:] if target.startswith(parent.rstrip("/") + "/") else ""
    return rest.split("/", 1)[0] if "/" in rest else ""
