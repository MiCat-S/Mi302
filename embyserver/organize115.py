"""整理 115 網盤：請 MoviePilot 看媒體庫裡每一部劇、每一部電影該叫什麼，名稱對不上的整個資料夾交給它整理。

Mi302 自己不判斷名稱對不對，也不猜 TMDB 編號、類型和季，全部問 MoviePilot：
- 找：每個劇集、電影資料夾問 MoviePilot「整理後叫什麼」（GET /api/v1/transfer/name；資料夾問一次，再挑一兩支影片問檔名），
  它用自己的辨識和重命名格式回答。和現在的名稱不一樣就列出來，附上 MoviePilot 給的名稱；旁邊已經有那個名稱的
  資料夾，就是會併進去。MoviePilot 的劇集格式有季資料夾、影片卻直接放在劇集資料夾裡的，也列出來。
  問過的記在資料庫（organize_checks），資料夾名稱和裡面的影片沒變就不再問。幾千個資料夾第一次要問一陣子，在背景跑。
- 預覽：整個資料夾交給 MoviePilot（有子資料夾的一個子資料夾一次，直接放著的影片一次），和它網頁「檔案管理 → 整理」
  一樣，影片、字幕、音軌一起整理到同一層。預設什麼都不指定，讓它自己認；它認錯的（例如名稱裡的「预计第二季度」
  會被認成第 2 季）再在那一部分指定類型、TMDB 編號或季。Mi302 只擋 MoviePilot 沒有擋的：新位置和原本一樣、或會搬出
  同步目錄的，整個部分不送；另外提醒兩個檔案整理到同一個位置、認的季和媒體庫不一樣。
執行、清掉搬空的舊資料夾、之後的增量同步沿用 reorganize.Reorganizer。
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
from .strm_sync import remote_root, task_key

log = logging.getLogger(__name__)

UNITS_TTL = 300  # 媒體庫裡有哪些資料夾，幾秒內直接用
LEVELS_META_KEY = "organize_levels"  # 上次檢查時 MoviePilot 的劇集、電影重命名格式各有幾層
PREVIEW_TIMEOUT = 600
SAMPLES = 2  # 一部劇挑幾支影片問 MoviePilot 檔名
MAX_WORKERS = 4  # 同時問 MoviePilot 幾個


class OrganizeError(Exception):
    pass


def _levels(template: str) -> int:
    """重命名格式有幾層（劇集預設三層：劇名資料夾／季資料夾／檔名；電影兩層）。"""
    return template.count("/") + 1 if template else 0


def _stem(name: str) -> str:
    return posixpath.splitext(name)[0]


# ---------------- 媒體庫裡的資料夾 ----------------


@dataclass
class Part:
    """一個資料夾要分幾次送：整個資料夾、每個子資料夾、直接放在資料夾裡的影片。"""

    key: str
    label: str
    cid: int  # 送整個資料夾時是它的 id；loose 時是放影片的那個資料夾
    rel: str  # 相對同步任務本機資料夾的路徑
    videos: int
    lib_season: Optional[int] = None  # 媒體庫裡這些集大多是第幾季（只拿來提醒，不送給 MoviePilot）
    loose: bool = False  # 只送直接放在這個資料夾裡的影片（旁邊還有子資料夾）
    stems: List[str] = field(default_factory=list)


@dataclass
class Unit:
    id: str  # d{資料夾 id}；沒有自己資料夾的電影是 f{檔案 id}
    kind: str  # series / movie / movie_file
    task: object
    rel: str
    path: str  # 115 上的完整路徑（movie_file 是影片，不含副檔名）
    cid: int  # 資料夾 id；movie_file 是它所在的資料夾
    parent_cid: int
    name: str
    item_id: int
    title: str
    year: Optional[int]
    videos: int
    loose: int  # 直接放在這個資料夾裡的影片數
    samples: List[str]  # 問 MoviePilot 檔名用的影片（115 路徑，副檔名是 .strm）
    siblings: List[str]  # 同一層的其他資料夾
    parts: List[Part] = field(default_factory=list)
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

    def view(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "name": self.name, "path": self.path, "parent": self.parent,
            "item_id": self.item_id, "title": self.title, "year": self.year, "videos": self.videos,
            "type": "movie" if self.kind != "series" else "tv", "reasons": self.reasons, "error": self.error,
            "mp_name": self.mp_name, "mp_files": self.mp_files, "merge_into": self.merge_into,
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
    """媒體庫裡在同步目錄底下的劇集資料夾、電影資料夾、沒有自己資料夾的電影（照 115 路徑排）。只看結構，不判斷名稱。"""
    units: List[Unit] = []
    for task in tasks:
        local = str(Path(task.local).expanduser())
        root = remote_root(task)
        tree = _Tree(db, task)
        lo, hi = local + "/", local + "/\U0010ffff"
        seasons: Dict[str, List[Optional[int]]] = {}  # 本機資料夾 → 裡面每一集的季
        for r in db.query("SELECT path, parent_index_number FROM items WHERE type='Episode' AND is_strm=1 AND path>=? AND path<?",
                          (lo, hi)):
            seasons.setdefault(posixpath.dirname(r["path"]), []).append(r["parent_index_number"])
        for s in db.query("SELECT id, name, year, path FROM items WHERE type='Series' AND path>=? AND path<?", (lo, hi)):
            rel = s["path"][len(lo):]
            if rel in tree.dirs and tree.total.get(rel):
                units.append(_series_unit(task, tree, s, rel, root, local, seasons))
        for m in db.query("SELECT id, name, year, path FROM items WHERE type='Movie' AND is_strm=1 AND path>=? AND path<?", (lo, hi)):
            unit = _movie_unit(task, tree, m, m["path"][len(lo):], root)
            if unit:
                units.append(unit)
    units.sort(key=lambda u: u.path)
    return units


def _series_unit(task, tree: _Tree, s, rel: str, root: str, local: str, seasons) -> Unit:
    cid, parent = tree.dirs[rel], posixpath.dirname(rel)
    loose = tree.strm.get(rel, [])
    subdirs = sorted(c for c in tree.children.get(rel, []) if tree.total.get(posixpath.join(rel, c)))

    def lib_season(folder_rel: str, only_here: bool) -> Optional[int]:
        folders = [folder_rel] if only_here else tree.walk(folder_rel)
        return _season_mode(v for f in folders for v in seasons.get(str(Path(local) / f), []))

    parts: List[Part] = []
    if not subdirs:
        parts.append(Part("all", "整個資料夾", cid, rel, len(loose), lib_season(rel, True)))
    else:
        for c in subdirs:
            c_rel = posixpath.join(rel, c)
            parts.append(Part(f"d{tree.dirs[c_rel]}", c, tree.dirs[c_rel], c_rel, tree.total[c_rel], lib_season(c_rel, False)))
        if loose:
            parts.append(Part("loose", "直接放在資料夾裡的影片", cid, rel, len(loose), lib_season(rel, True),
                              loose=True, stems=[_stem(n) for _, n in loose]))
    return Unit(f"d{cid}", "series", task, rel, posixpath.join(root, rel), cid, tree.dirs.get(parent, 0),
                posixpath.basename(rel), s["id"], s["name"], s["year"], tree.total[rel], len(loose),
                _samples(tree.videos_under(rel), root), [c for c in tree.children.get(parent, []) if c != posixpath.basename(rel)],
                parts)


def _movie_unit(task, tree: _Tree, m, strm_rel: str, root: str) -> Optional[Unit]:
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
        return Unit(f"d{cid}", "movie", task, folder, posixpath.join(root, folder), cid, tree.dirs.get(parent, 0),
                    posixpath.basename(folder), m["id"], m["name"], m["year"], 1, 1, [posixpath.join(root, strm_rel)],
                    [c for c in tree.children.get(parent, []) if c != posixpath.basename(folder)],
                    [Part("all", "整個資料夾", cid, folder, 1)])
    cid = tree.dirs.get(folder, 0)
    return Unit(f"f{fid}", "movie_file", task, strm_rel, posixpath.join(root, folder, _stem(strm_name)), cid,
                tree.dirs.get(posixpath.dirname(folder), 0) if folder else 0, _stem(strm_name), m["id"], m["name"], m["year"],
                1, 1, [posixpath.join(root, strm_rel)], tree.children.get(folder, []),
                [Part("file", "這支影片", cid, folder, 1, loose=True, stems=[_stem(strm_name)])])


def judge(unit: Unit, tv_levels: int, movie_levels: int) -> None:
    """依 MoviePilot 的回答決定這個資料夾要不要整理，原因寫進 unit.reasons。"""
    unit.reasons, unit.merge_into = [], None
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
    bad = [f for f in unit.mp_files if _stem(f[0]) != _stem(f[1])]
    if bad:
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
    found: int = 0  # 目前不規範的
    current: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class Organizer:
    def __init__(self, db: Database, strm_sync, moviepilot: MoviePilot, reorganizer):
        self.db = db
        self.strm_sync = strm_sync
        self.mp = moviepilot
        self.reorg = reorganizer
        self.job = CheckJob()
        self._lock = threading.Lock()  # 同時只跑一個檢查
        self._units: Dict[str, Unit] = {}
        self._order: List[str] = []
        self._units_at = 0.0
        try:
            tv, movie = json.loads(db.get_meta(LEVELS_META_KEY) or "[3, 2]")
            self._levels: Tuple[int, int] = (int(tv), int(movie))
        except (ValueError, TypeError):
            self._levels = (3, 2)

    @property
    def p115(self):
        return self.strm_sync.p115

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
        if refresh or not self._units_at or time.time() - self._units_at > UNITS_TTL:
            units = find_units(self.db, self.strm_sync.tasks)
            self._units = {u.id: u for u in units}
            self._order = [u.id for u in units]
            self._units_at = time.time()
            self._apply_cache()
        return [self._units[i] for i in self._order]

    def _apply_cache(self) -> None:
        cached = {r["path"]: r for r in self.db.query("SELECT * FROM organize_checks")}
        tv, movie = self._levels
        for u in self._units.values():
            r = cached.get(u.path)
            if r and r["sig"] == u.sig:
                u.checked, u.mp_name, u.error = True, r["name"] or "", r["error"] or ""
                u.mp_files = json.loads(r["files"] or "[]")
                judge(u, tv, movie)
            else:
                u.checked, u.reasons = False, []

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
            job.found = sum(1 for u in units if u.checked and u.reasons and u.id not in asking)
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
                    job.found += 1 if u.reasons else 0
            live = {u.path for u in units}
            with self.db.lock:
                old = [r["path"] for r in self.db.query("SELECT path FROM organize_checks")]
                self.db.conn.executemany("DELETE FROM organize_checks WHERE path=?", [(p,) for p in old if p not in live])
                self.db.conn.commit()
            log.info("整理 115 網盤：問完 %s 個，%s 個不照 MoviePilot 的格式", job.done, job.found)
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

    # ---- 清單 ----

    def list(self, q: str = "", kind: str = "", offset: int = 0, limit: int = 50) -> dict:
        units = self.units()
        found = [u for u in units if u.checked and u.reasons]
        counts = Counter("series" if u.kind == "series" else "movie" for u in found)
        text = q.strip().casefold()
        shown = [u for u in found if (not kind or (u.kind == "series") == (kind == "series"))
                 and (not text or text in u.name.casefold() or text in (u.title or "").casefold() or text in u.path.casefold())]
        return {
            "job": self.job.as_dict(), "total": len(shown), "folders": len(units),
            "unchecked": sum(1 for u in units if not u.checked),
            "counts": {"series": counts["series"], "movie": counts["movie"]},
            "items": [u.view() for u in shown[offset:offset + limit]],
        }

    def nonstandard_series(self) -> Set[int]:
        """問過 MoviePilot、資料夾不照格式的劇（給「集號不對的劇」標出來）；沒問過的不算，也不會因此去問。"""
        return {u.item_id for u in self.units() if u.kind == "series" and u.checked and u.reasons}

    # ---------------- 預覽 ----------------

    def preview(self, unit_id: str, overrides: Dict[str, dict], scrape: bool = True) -> dict:
        """請 MoviePilot 只算不做，一個部分一次。overrides：{部分: {type, tmdbid, season}}，沒給的讓 MoviePilot 自己認。"""
        unit = next((u for u in self.units() if u.id == unit_id), None)
        if not unit:
            raise OrganizeError("清單已經更新，找不到這個資料夾，請重新整理")
        try:
            self.mp.check_transfer_preview()  # 舊版 MoviePilot 會把預覽當成真的整理
        except MoviePilotError as exc:
            raise OrganizeError(str(exc))
        roots = [remote_root(t) for t in self.strm_sync.tasks]
        items: List[dict] = []
        batches: List[dict] = []
        notes: List[str] = []
        parts: List[dict] = []
        listings: Dict[int, List[dict]] = {}
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
                results = self.mp.transfer(fileitems, o["tmdbid"] or None, o["season"], None, scrape, unit.parent, preview=True,
                                           mtype=o["type_name"], timeout=PREVIEW_TIMEOUT, single=single)
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
            blocked = [v for v in views if v["blocked"]]
            if blocked:
                notes.append(f"「{part.label}」有 {len(blocked)} 個檔案不能整理（{blocked[0]['message']}），"
                             "MoviePilot 是整個資料夾一起整理，所以這一部分都不送")
                for v in views:
                    v["ok"] = False
            items += views
            ok = sum(1 for v in views if v["ok"])
            parts.append({"key": part.key, "label": part.label, "recognized": recognized, "ok": ok})
            if ok:
                batches.append({"fileitems": fileitems, "single": single, "season": o["season"], "tmdbid": o["tmdbid"],
                                "type_name": o["type_name"], "count": len(views), "label": part.label,
                                "local": str(Path(unit.task.local).expanduser() / part.rel)})
        folders = sorted({_top_folder(v["target"], unit.parent) for v in items if v["ok"] and v["target"]} - {""})
        token = self._remember(unit, batches, scrape) if batches else None
        return {
            "token": token, "items": items, "notes": notes, "folders": folders, "parts": parts,
            "summary": {"total": len(items), "ok": sum(1 for i in items if i["ok"]),
                        "failed": sum(1 for i in items if not i["ok"]),
                        "warnings": sum(1 for i in items if i["ok"] and i["warnings"])},
        }

    def _fileitems(self, unit: Unit, part: Part, listings: Dict[int, List[dict]]) -> Tuple[List[dict], bool]:
        """這一部分要送給 MoviePilot 的項目：整個資料夾一個（single），直接放著的影片照 115 上的檔名一個一個。"""
        remote = posixpath.join(remote_root(unit.task), part.rel) if part.rel else remote_root(unit.task)
        if not part.loose:
            name = posixpath.basename(remote)
            parent_cid = unit.parent_cid if part.cid == unit.cid else unit.cid
            return [{"storage": "u115", "type": "dir", "path": remote.rstrip("/") + "/", "name": name, "basename": name,
                     "fileid": str(part.cid), "parent_fileid": str(parent_cid)}], True
        if part.cid not in listings:
            listings[part.cid] = self.p115.list_dir(part.cid)
        wanted = set(part.stems)
        out = []
        for e in listings[part.cid]:
            stem, ext = posixpath.splitext(e["name"])
            if not e["is_dir"] and ext.lower() in VIDEO_EXTS and stem in wanted:
                out.append({"storage": "u115", "type": "file", "path": posixpath.join(remote, e["name"]), "name": e["name"],
                            "basename": stem, "extension": ext.lstrip(".").lower(), "size": int(e.get("size") or 0),
                            "fileid": str(e["id"]), "parent_fileid": str(part.cid), "pickcode": e.get("pickcode") or ""})
        return out, len(out) == 1

    def _remember(self, unit: Unit, batches: List[dict], scrape: bool) -> str:
        payload = {"plan_id": f"o{unit.id}", "mode": "organize", "title": unit.path,
                   "cid": unit.cid if unit.kind != "movie_file" else None, "tmdbid": "", "type_name": None, "season": None,
                   "target_path": unit.parent, "scrape": bool(scrape), "batches": batches, "files": {}}
        token = hashlib.sha1(json.dumps({k: payload[k] for k in ("plan_id", "scrape", "batches")},
                                        sort_keys=True, default=str).encode()).hexdigest()[:16]
        self.reorg.remember_preview(token, payload)
        return token


def _override(o: dict) -> dict:
    """網頁上某一部分的指定：類型 tv／movie、TMDB 編號、季；空的讓 MoviePilot 自己認。"""
    tmdbid = str(o.get("tmdbid") or "").strip()
    if tmdbid and not tmdbid.isdigit():
        raise OrganizeError("TMDB 編號要是數字")
    season = o.get("season")
    season = int(season) if str(season if season is not None else "").strip().isdigit() else None
    type_name = {"tv": "电视剧", "movie": "电影"}.get(str(o.get("type") or ""))
    if type_name == "电影":
        season = None
    return {"tmdbid": tmdbid, "season": season, "type_name": type_name}


def _view(r: dict, part: Part, roots: List[str]) -> dict:
    """MoviePilot 預覽的一個檔案，加上 Mi302 的檢查（MoviePilot 沒有擋來源等於目標，也不知道同步目錄在哪）。"""
    source = str(r.get("source") or "")
    target = str(r.get("target") or r.get("target_dir") or "")
    episode = int(r["episode"]) if str(r.get("episode") or "").isdigit() else None
    season = int(r["season"]) if str(r.get("season") if r.get("season") is not None else "").isdigit() else None
    ok = bool(r.get("success")) and bool(target)
    message, warnings, blocked = str(r.get("message") or ""), [], False
    if ok and not any(target.startswith(root.rstrip("/") + "/") for root in roots):
        ok, blocked, message = False, True, "新位置不在 Mi302 的 115 同步目錄裡，整理後會從媒體庫消失"
    elif ok and target.rstrip("/") == source.rstrip("/"):
        ok, blocked, message = False, True, "新位置和原本一樣（已經照格式命名）"
    if not ok and not message:
        message = "MoviePilot 沒有說明原因"
    return {"name": posixpath.basename(source.rstrip("/")), "source": source, "target": target, "episode": episode,
            "season": season, "title": str(r.get("title") or ""), "type": str(r.get("type") or ""), "ok": ok,
            "blocked": blocked, "message": message, "warnings": warnings, "part": part.label}


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
