"""115 上的空資料夾：底下沒有影音檔的資料夾找出來，勾選後移到 115 回收站。

詳細說明見 docs/modules.md 的「embyserver/emptydirs.py」。
"""

from __future__ import annotations

import json
import logging
import posixpath
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import deletelog
from .db import Database
from .dupes import DELETE_BATCH
from .filetypes import MEDIA_EXTS
from .moviepilot import MoviePilotError
from .p115 import P115Error, P115Throttled
from .strm_sync import outer_roots, remote_root
from .workers import Stopped, Workers

log = logging.getLogger(__name__)

SCAN_META_KEY = "empty_dirs_scan"
RECENT = 3600  # 一小時內有變動的資料夾先不列、不刪（可能還在整理或下載）
SAMPLE = 5  # 每個資料夾記下幾個裡面的檔名給網頁看
BIG = 100 << 20  # 裡面的檔案加起來超過這麼大，網頁上提醒看一下（不只是 nfo、圖片）
RESULTS = 200  # 刪除時最多記幾個沒刪的原因
PACE = 1.0  # 一個一個列資料夾時至少間隔幾秒（同步設定的 request_delay 比較大就照它）
# 藍光、DVD 原盤的資料夾：BDMV 裡沒有影片的 CLIPINF、PLAYLIST，和 BDMV 並排的 CERTIFICATE 都是原盤的一部分
DISC_DIRS = {"bdmv", "video_ts", "audio_ts", "hvdvd_ts", "certificate", "aacs", "bdav"}
DISC_ROOTS = {"bdmv", "video_ts"}


class EmptyDirsError(Exception):
    pass


def is_media(name: str) -> bool:
    return posixpath.splitext(name)[1].lower() in MEDIA_EXTS


def _join(root: str, rel: str) -> str:
    return posixpath.join(root, rel) if rel else root


def classify(tree: Iterable[Tuple[str, ...]], file_names: Set[str]) -> Dict[str, bool]:
    """導出的目錄樹（只有名稱）→ {相對路徑: 是不是資料夾}。底下還有東西的是資料夾；最底層的，115 列出的檔案裡有這個
    名稱、或是影音檔的算檔案，其他的當成空資料夾（看錯的，之後列上一層時就知道了）。"""
    tree = list(tree)
    folders = {"/".join(p[:k]) for p in tree for k in range(1, len(p))}
    out: Dict[str, bool] = {}
    for parts in tree:
        rel = "/".join(parts)
        out[rel] = rel in folders or not (parts[-1] in file_names or is_media(parts[-1]))
    return out


def find_empty(nodes: Dict[str, bool]) -> List[str]:
    """{相對路徑: 是不是資料夾} 裡底下沒有影音檔的資料夾，只留最外層的（上一層有影音檔，或上一層就是範圍本身）；
    藍光、DVD 原盤裡的不算。照路徑排。"""
    media: Set[str] = {""}  # 底下有影音檔的資料夾；範圍本身一定不列
    for rel, is_dir in nodes.items():
        if not is_dir and is_media(rel):
            d = posixpath.dirname(rel)
            while d not in media:
                media.add(d)
                d = posixpath.dirname(d)
    discs = {posixpath.dirname(rel) for rel, is_dir in nodes.items()
             if is_dir and posixpath.basename(rel).casefold() in DISC_ROOTS}  # 放原盤的資料夾

    def disc(rel: str) -> bool:
        return posixpath.dirname(rel) in discs or any(p.casefold() in DISC_DIRS for p in rel.split("/"))

    return sorted(rel for rel, is_dir in nodes.items()
                  if is_dir and rel not in media and posixpath.dirname(rel) in media and not disc(rel))


def contents(nodes: Dict[str, bool], cands: Iterable[str]) -> Dict[str, Tuple[List[str], int]]:
    """每個資料夾裡有什麼（從目錄樹看，不用再列）：檔案（相對於它的路徑，照名稱排）和子資料夾數。"""
    files: Dict[str, List[str]] = {c: [] for c in cands}
    dirs: Counter = Counter()
    for rel, is_dir in nodes.items():
        d = posixpath.dirname(rel)
        while d and d not in files:
            d = posixpath.dirname(d)
        if d in files:
            if is_dir:
                dirs[d] += 1
            else:
                files[d].append(rel[len(d) + 1:])
    return {c: (sorted(names), dirs[c]) for c, names in files.items()}


@dataclass
class EmptyJob:
    """掃描或刪除的進度；網頁每幾秒查一次。"""

    kind: str = ""  # scan = 掃描、delete = 刪除
    running: bool = False
    started: float = 0.0
    finished: float = 0.0
    listed: int = 0  # 掃描：目錄樹裡有幾個項目
    found: int = 0  # 掃描：從名稱看起來是空的，要到 115 上確認幾個
    checked: int = 0  # 掃描：確認過幾個
    total: int = 0  # 刪除：勾了幾個
    done: int = 0  # 刪除：送進 115 回收站幾個
    freed: int = 0  # 刪除：裡面的檔案共多大
    kept: int = 0  # 刪除：再確認時不能刪的
    stopped: bool = False  # 按了停止
    current: str = ""
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    results: List[dict] = field(default_factory=list)  # 刪除時沒刪的：path、why（最多 RESULTS 個）

    def as_dict(self) -> dict:
        return asdict(self)


class EmptyDirs:
    def __init__(self, db: Database, strm_sync, moviepilot, reorganizer, scanner=None):
        self.db = db
        self.strm_sync = strm_sync
        self.mp = moviepilot
        self.reorg = reorganizer
        self.scanner = scanner
        self.job = EmptyJob()
        self._lock = threading.Lock()  # 掃描、刪除同時只做一個
        self.workers = Workers(busy=self._lock)  # 程式結束時停在兩個資料夾之間
        self._stop = self.workers.stop
        self.pace = PACE

    @property
    def p115(self):
        return self.strm_sync.p115

    def stop(self) -> None:
        self._stop.set()

    def cancel(self) -> bool:
        """按了停止：掃描停下後保留上次的結果；刪除做完手上這一個就停。沒在做回傳 False。"""
        if not self.job.running:
            return False
        self.workers.cancel.set()
        return True

    def default_roots(self) -> List[str]:
        return outer_roots(self.strm_sync.tasks)

    def _pace(self) -> None:
        """每列一個 115 目錄之前等一下：至少 pace 秒，同步設定的 request_delay 比較大就照它，熔斷剛恢復時再放慢。
        按了停止或程式要結束就丟 Stopped。"""
        delay = max(getattr(self.strm_sync.cfg, "request_delay", 0) or 0, self.pace) * self.p115.breaker.slowdown()
        if delay:
            self.workers.wait(delay)
        self.workers.check()

    # ---------------- 狀態與清單 ----------------

    def summary(self) -> dict:
        meta = json.loads(self.db.get_meta(SCAN_META_KEY) or "{}")
        stats = self.db.one("SELECT COUNT(*) AS c, COALESCE(SUM(size), 0) AS s FROM empty_dirs")
        return {"job": self.job.as_dict(), "scanned_at": meta.get("at"), "roots": meta.get("roots") or [],
                "skipped": meta.get("skipped") or {}, "count": stats["c"], "size": stats["s"],
                "default_roots": self.default_roots()}

    @staticmethod
    def _where(q: str) -> Tuple[str, list]:
        return ("WHERE path LIKE ?", [f"%{q.strip()}%"]) if q.strip() else ("", [])

    def items(self, q: str = "", offset: int = 0, limit: int = 50) -> dict:
        """空資料夾照路徑排，q 比對路徑；附上符合的有幾個、裡面的檔案共多大。id 用字串給網頁（115 的 id 超過 JS 整數）。"""
        where, params = self._where(q)
        stats = self.db.one(f"SELECT COUNT(*) AS c, COALESCE(SUM(size), 0) AS s FROM empty_dirs {where}", params)
        rows = self.db.query(f"SELECT * FROM empty_dirs {where} ORDER BY path LIMIT ? OFFSET ?", (*params, limit, offset))
        return {"total": stats["c"], "size": stats["s"], "items": [
            {"cid": str(r["cid"]), "path": r["path"], "files": r["files"], "dirs": r["dirs"], "size": r["size"],
             "big": r["size"] >= BIG, "sample": json.loads(r["sample"] or "[]"), "mtime": r["mtime"]} for r in rows]}

    def plan(self, overrides: Dict[int, bool], select_all: bool = False, q: str = "") -> List[dict]:
        """要刪哪些：overrides 裡勾了的；select_all 時再加上符合搜尋的全部（overrides 取消勾的除外）。"""
        rows: Dict[int, dict] = {}
        if select_all:
            where, params = self._where(q)
            rows = {r["cid"]: dict(r) for r in self.db.query(f"SELECT * FROM empty_dirs {where}", params)
                    if overrides.get(r["cid"], True)}
        extra = [cid for cid, on in overrides.items() if on and cid not in rows]
        for start in range(0, len(extra), 500):
            chunk = extra[start:start + 500]
            rows.update((r["cid"], dict(r)) for r in self.db.query(
                f"SELECT * FROM empty_dirs WHERE cid IN ({','.join('?' * len(chunk))})", chunk))
        return sorted(rows.values(), key=lambda r: r["path"])

    # ---------------- 掃描 ----------------

    def scan_in_background(self, roots: Optional[List[str]] = None) -> bool:
        """在背景掃描；已經在掃描或刪除回傳 False。同步時不掃：兩邊都要用 115 的導出目錄樹，同時只能有一個。"""
        if not self.p115.cookies:
            raise EmptyDirsError("找空資料夾要用掃碼登入（cookie）：要用 115 的導出目錄樹")
        if self.strm_sync.result.running:
            raise EmptyDirsError("115 正在同步，等同步完成再掃描")
        if not self._lock.acquire(blocking=False):
            return False
        self.workers.cancel.clear()
        self.job = EmptyJob(kind="scan", running=True, started=time.time())
        self.workers.start(self._scan, roots)
        return True

    def _scan(self, roots: Optional[List[str]]) -> None:
        job = self.job
        try:
            roots = ["/" + r.strip("/") for r in roots or [] if str(r).strip()] or self.default_roots()
            if not roots:
                raise P115Error("還沒有同步任務，請選一個 115 目錄")
            protected = self._protected(job)
            skipped: Counter = Counter()
            rows: List[tuple] = []
            for root in self._expand(roots):
                rows += self._scan_root(root, job, protected, skipped)
            self.workers.check()  # 換掉上次的結果之前再看一次：按了停止就保留上次的
            with self.db.lock:
                self.db.conn.execute("DELETE FROM empty_dirs")
                self.db.conn.executemany(
                    "INSERT OR REPLACE INTO empty_dirs(cid, parent_cid, path, files, dirs, size, sample, mtime) "
                    "VALUES(?,?,?,?,?,?,?,?)", rows)
                self.db.conn.commit()
            self.db.set_meta(SCAN_META_KEY, json.dumps({"at": int(time.time()), "roots": roots, "skipped": dict(skipped)},
                                                       ensure_ascii=False))
            log.info("找空資料夾：%s 有 %s 個（另外沒列的：%s）", "、".join(roots), len(rows), dict(skipped) or "無")
        except Stopped:
            job.stopped = self.workers.by_user
            job.errors.append(f"{'按了停止' if job.stopped else '程式要結束'}，掃描中途停下；上次的結果沒有換掉")
        except P115Error as exc:
            job.errors.append(str(exc))
            log.warning("找空資料夾失敗：%s", exc)
        except Exception as exc:  # 不能讓背景執行緒默默死掉，網頁要看得到原因
            job.errors.append(f"{type(exc).__name__}: {exc}")
            log.exception("找空資料夾時發生未預期的錯誤")
        finally:
            job.running = False
            job.current = ""
            job.finished = time.time()
            self._lock.release()

    def _protected(self, job: EmptyJob) -> Set[str]:
        """不列的資料夾：同步任務的目錄、MoviePilot 目錄設定裡存儲是 115 的下載目錄和媒體庫目錄，以及它們的上層。"""
        paths = [remote_root(t) for t in self.strm_sync.tasks]
        if self.mp.enabled:
            try:
                for d in self.mp.library_dirs():
                    if d.get("storage") == "u115" and d.get("download_path"):
                        paths.append(str(d["download_path"]))
                    if d.get("library_storage") == "u115" and d.get("library_path"):
                        paths.append(str(d["library_path"]))
            except MoviePilotError as exc:
                job.notes.append(f"讀不到 MoviePilot 的目錄設定（{exc}），它的下載目錄、媒體庫目錄是空的話也會列出來")
        out: Set[str] = set()
        for p in paths:
            p = "/" + p.strip().strip("/")
            while p != "/" and p not in out:
                out.add(p)
                p = posixpath.dirname(p)
        return out

    def _expand(self, roots: List[str]) -> List[str]:
        """範圍是整個網盤時，最上層的資料夾一個一個掃（它們本身不列，也不知道導出整個網盤的目錄樹 115 收不收）。"""
        out: List[str] = []
        for root in roots:
            if root == "/":
                self.p115.breaker.check()
                out += ["/" + e["name"] for e in self.p115.list_dir(0) if e["is_dir"]]
            else:
                out.append(root)
        return list(dict.fromkeys(out))

    def _snapshot(self, root: str, cid: int, job: EmptyJob) -> Tuple[Dict[str, bool], Dict[int, List[dict]]]:
        """範圍裡每個項目 {相對路徑: 是不是資料夾}（導出目錄樹），和 115 列出的檔案照所在資料夾 id 分好。"""
        job.current = f"{root}：等 115 導出目錄樹"
        tree = self.p115.export_tree(cid, root)
        job.listed += len(tree)
        by_parent: Dict[int, List[dict]] = defaultdict(list)
        names: Set[str] = set()
        for n, info in enumerate(self.p115.iter_changed_files(cid, 0), 1):  # since = 0：全部
            self.workers.check()
            names.add(info["name"])
            by_parent[info["parent_id"]].append(info)
            if n % 1000 == 1:
                job.current = f"{root}：列出檔案（{n} 個）"
        return classify(tree, names), by_parent

    def _scan_root(self, root: str, job: EmptyJob, protected: Set[str], skipped: Counter) -> List[tuple]:
        self.p115.breaker.check()
        job.current = root
        cid = self.p115.dir_id(root)
        try:
            nodes, by_parent = self._snapshot(root, cid, job)
        except P115Throttled:
            raise
        except P115Error as exc:
            log.warning("找空資料夾：%s 導出目錄樹失敗：%s", root, exc)
            job.notes.append(f"{root}：115 導出目錄樹失敗（{exc}），這個目錄這次沒掃（清單上留著上次的結果），等一下再掃描")
            return [tuple(r) for r in self.db.query(
                "SELECT cid, parent_cid, path, files, dirs, size, sample, mtime FROM empty_dirs WHERE substr(path, 1, ?)=?",
                (len(root) + 1, root.rstrip("/") + "/"))]
        cands = []
        for rel in find_empty(nodes):
            path = _join(root, rel)
            if path in protected or path.count("/") <= 1:  # 115 最上層的資料夾也不列
                skipped["protected"] += 1
            else:
                cands.append(rel)
        job.found += len(cands)
        inside = contents(nodes, cands)
        groups: Dict[str, List[str]] = {}
        for rel in cands:
            groups.setdefault(posixpath.dirname(rel), []).append(rel)
        rows: List[tuple] = []
        for parent, rels in sorted(groups.items()):
            self.p115.breaker.check()
            self._pace()
            job.current = _join(root, parent)
            try:
                pid = self.p115.dir_id(_join(root, parent)) if parent else cid
                entries = {e["name"]: e for e in self.p115.list_dir(pid) if e["is_dir"]}
            except P115Throttled:
                raise
            except P115Error as exc:  # 導出之後才搬走、刪掉的：這幾個不列
                log.info("找空資料夾：列不出 %s：%s", _join(root, parent), exc)
                job.checked += len(rels)
                continue
            for rel in rels:
                job.checked += 1
                e = entries.get(posixpath.basename(rel))
                if not e:
                    continue  # 目錄樹最底層的其實是檔案，或已經不在了
                if e["mtime"] and time.time() - e["mtime"] < RECENT:
                    skipped["recent"] += 1
                    continue
                files, dirs = inside[rel]
                if any("/" in f for f in files):  # 子資料夾裡也有檔案：整個列一遍才知道多大
                    self._pace()
                    job.current = _join(root, rel)
                    content = self._content(e["id"])
                else:  # 檔案都直接放在裡面：115 列出的檔案裡就有大小
                    direct = by_parent.get(e["id"], [])
                    content = None if any(is_media(f["name"]) for f in direct) else {
                        "files": len(files), "dirs": dirs, "size": sum(int(f.get("size") or 0) for f in direct),
                        "sample": files[:SAMPLE]}
                if content is None:
                    skipped["media"] += 1
                    continue
                rows.append((e["id"], pid, _join(root, rel), content["files"], content["dirs"], content["size"],
                             json.dumps(content["sample"], ensure_ascii=False), e["mtime"] or None))
        return rows

    def _content(self, cid: int) -> Optional[dict]:
        """資料夾（含子資料夾）裡有什麼：檔案數、資料夾數、大小、前幾個檔名；有影音檔回傳 None。"""
        files, dirs, size, names = 0, 0, 0, []
        stack = [(cid, "")]
        while stack:
            folder, rel = stack.pop()
            for e in self.p115.list_dir(folder):
                name = f"{rel}/{e['name']}" if rel else e["name"]
                if e["is_dir"]:
                    dirs += 1
                    stack.append((e["id"], name))
                elif is_media(e["name"]):
                    return None
                else:
                    files += 1
                    size += int(e.get("size") or 0)
                    names.append(name)
            if stack:
                self._pace()
        return {"files": files, "dirs": dirs, "size": size, "sample": sorted(names)[:SAMPLE]}

    # ---------------- 刪除 ----------------

    def delete_in_background(self, plan: List[dict]) -> None:
        """勾的資料夾在背景一個一個再確認、送進 115 回收站。進度看 job。"""
        if not plan:
            raise EmptyDirsError("沒有勾要刪的資料夾")
        if not self.p115.cookies:
            raise EmptyDirsError("刪除 115 上的檔案要用掃碼登入（cookie）")
        if self.strm_sync.result.running:
            raise EmptyDirsError("115 正在同步，等同步完成再刪")
        if not self._lock.acquire(blocking=False):
            raise EmptyDirsError("正在掃描或刪除空資料夾，等它做完")
        if not self.reorg.hold():
            self._lock.release()
            raise EmptyDirsError("MoviePilot 正在整理（或正在刪除別的），等它完成再刪")
        self.workers.cancel.clear()
        self.job = EmptyJob(kind="delete", running=True, started=time.time(), total=len(plan))
        self.workers.start(self._delete, plan)

    def _delete(self, plan: List[dict]) -> None:
        job = self.job
        rescan: List[str] = []
        try:
            groups: Dict[int, List[dict]] = {}
            for r in plan:
                groups.setdefault(r["parent_cid"], []).append(r)
            for parent, rows in groups.items():
                self.p115.breaker.check()
                self._pace()
                folder = posixpath.dirname(rows[0]["path"])
                job.current = f"{folder}：重新導出目錄樹確認"
                try:
                    # 上一層底下有影音檔的資料夾（名稱）；重新導出一次，不必一個一個列
                    media = {p[0] for p in self.p115.export_tree(parent, folder) if is_media(p[-1])}
                    self._pace()
                    entries = {e["id"]: e for e in self.p115.list_dir(parent)}
                except P115Throttled:
                    raise
                except P115Error as exc:  # 上一層不在了、115 正在導出別的目錄樹…：這一組先不刪
                    for r in rows:
                        self._kept(job, r, f"重新確認時 115 出錯（{exc}），這次沒刪，之後再試")
                    continue
                ok = []
                for r in rows:
                    why = self._recheck(r, entries.get(r["cid"]), media)
                    if why:
                        self._kept(job, r, why)
                    else:
                        ok.append(r)
                for start in range(0, len(ok), DELETE_BATCH):
                    if start:
                        self._pace()
                    batch = ok[start:start + DELETE_BATCH]
                    self.p115.delete_files([r["cid"] for r in batch])
                    deletelog.record(self.db, "empty", ({"file_id": r["cid"], "path": r["path"], "is_dir": True} for r in batch))
                    job.done += len(batch)
                    job.freed += sum(r["size"] for r in batch)
                    rescan += self._after_delete(batch)
                    log.info("空資料夾移到 115 回收站：%s", "、".join(r["path"] for r in batch))
            log.info("刪空資料夾：%s 個移到 115 回收站，%s 個再確認時沒刪", job.done, job.kept)
        except Stopped:
            job.stopped = self.workers.by_user
            left = job.total - job.done - job.kept
            job.errors.append(f"{'按了停止' if job.stopped else '程式要結束'}，還有 {left} 個沒處理")
        except P115Error as exc:
            job.errors.append(f"處理到 {job.current} 時失敗：{exc}")
            log.warning("刪空資料夾失敗：%s", exc)
        except Exception as exc:
            job.errors.append(f"{type(exc).__name__}: {exc}")
            log.exception("刪空資料夾時發生未預期的錯誤")
        finally:
            job.current = "重新掃描媒體庫" if rescan else ""
            try:
                if rescan and self.scanner:
                    self.scanner.scan_paths(rescan)  # 這些地方的劇、電影（影片早就搬走了）從媒體庫拿掉
            finally:
                job.running = False
                job.current = ""
                job.finished = time.time()
                self.reorg.release()
                self._lock.release()

    @staticmethod
    def _kept(job: EmptyJob, r: dict, why: str) -> None:
        job.kept += 1
        if len(job.results) < RESULTS:
            job.results.append({"path": r["path"], "why": why})

    def _recheck(self, r: dict, entry: Optional[dict], media: Set[str]) -> str:
        """刪之前再看一次（entry：剛列出的上一層裡的它；media：剛導出的目錄樹裡底下有影音檔的資料夾名稱）。
        可以刪回傳空字串，不行回傳原因；已經不是這個資料夾、不再是空的就從清單拿掉。"""
        name = posixpath.basename(r["path"])
        if not entry or not entry["is_dir"] or entry["name"] != name:
            self.db.execute("DELETE FROM empty_dirs WHERE cid=?", (r["cid"],))
            return "已經不在原本的位置（搬走、改名或刪掉了），沒有動它"
        if entry["mtime"] and time.time() - entry["mtime"] < RECENT:
            return "一小時內剛有變動（可能正在整理或下載），先不刪；之後重新掃描再看"
        if name in media:
            self.db.execute("DELETE FROM empty_dirs WHERE cid=?", (r["cid"],))
            return "現在裡面有影音檔了，沒有刪"
        return ""

    def _after_delete(self, batch: List[dict]) -> List[str]:
        """115 上已經刪了：本機對應的資料夾（Mi302 下載的 nfo、圖片）和同步紀錄拿掉，從清單拿掉；回傳要重新掃描的位置。"""
        removed = self.strm_sync.remove_local([r["cid"] for r in batch])  # 同步紀錄裡有的
        for r in batch:
            local = self._local_of(r["path"])
            if local:
                removed += self.strm_sync.clear_local_dir(local) + [local]  # 同步紀錄裡沒有的（例如一直是空的）
        self.db.executemany("DELETE FROM empty_dirs WHERE cid=?", [(r["cid"],) for r in batch])
        return removed

    def _local_of(self, path: str) -> Optional[str]:
        """115 路徑在本機對應的位置（不在任何同步任務裡是 None）。"""
        for t in self.strm_sync.tasks:
            root = remote_root(t)
            if root == "/" or path.startswith(root.rstrip("/") + "/"):
                return str(Path(t.local).expanduser() / path[len(root):].lstrip("/"))
        return None
