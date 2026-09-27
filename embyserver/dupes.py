"""115 上內容完全相同的影片：找出來、建議保留哪一份、刪掉多的（刪重複的第一階段）。

- 找：列出範圍內所有檔案（預設是同步任務的 115 目錄，也可以另選），只看影片；SHA1 和大小都一樣的是同一組。
  115 列檔案時本來就附上每個檔案的 SHA1，四萬多個檔案大約四十次請求。
- 建議保留：本機有 strm 的那份優先（刮削資料、觀看紀錄都在它身上），其次最早上傳的。
- 刪：送進 115 回收站（在 115 還原得回來），每組至少留一份。本機的 strm 和同名的中繼資料跟著刪，
  觀看紀錄轉到保留的那份，再重新掃描受影響的劇或電影。刪過的記在 dup_deleted，方便到回收站找回。
"""

from __future__ import annotations

import json
import logging
import posixpath
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .db import Database
from .filetypes import VIDEO_EXTS
from .p115 import P115Error, P115Service

log = logging.getLogger(__name__)

SCAN_META_KEY = "dupes_scan"
DELETE_BATCH = 100  # 一次請求送幾個檔案進回收站
MAX_ERRORS = 20


@dataclass
class DupeJob:
    """找重複或刪重複的進度；網頁每幾秒查一次。"""

    kind: str = ""  # scan = 找重複、delete = 刪重複
    running: bool = False
    started: float = 0.0
    finished: float = 0.0
    listed: int = 0  # 找重複：看過幾個 115 檔案
    total: int = 0  # 刪重複：要刪幾個
    done: int = 0  # 刪重複：已經刪了幾個
    freed: int = 0  # 刪重複：省下幾個位元組
    current: str = ""
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class DupeFinder:
    def __init__(self, db: Database, p115: P115Service, strm_sync, scanner):
        self.db = db
        self.p115 = p115
        self.strm_sync = strm_sync
        self.scanner = scanner
        self.job = DupeJob()
        self._lock = threading.Lock()

    # ---------------- 範圍與狀態 ----------------

    def default_roots(self) -> List[str]:
        """同步任務的 115 目錄；互相包含的只留外層，免得同一個檔案列兩次。"""
        roots = sorted({"/" + t.remote.strip("/") for t in self.strm_sync.tasks}, key=len)
        out: List[str] = []
        for root in roots:
            if not any(root == o or root.startswith(o.rstrip("/") + "/") for o in out):
                out.append(root)
        return out

    def summary(self) -> dict:
        meta = json.loads(self.db.get_meta(SCAN_META_KEY) or "{}")
        stats = self.db.one(
            "SELECT COUNT(*) AS files, COUNT(DISTINCT sha1 || ':' || size) AS groups, "
            "COALESCE(SUM(CASE WHEN keep THEN 0 ELSE size END), 0) AS reclaimable FROM dup_files"
        )
        return {
            "job": self.job.as_dict(),
            "scanned_at": meta.get("at"),
            "roots": meta.get("roots") or [],
            "scanned_files": meta.get("files", 0),
            "groups": stats["groups"],
            "files": stats["files"],
            "reclaimable": stats["reclaimable"],  # 照建議刪掉可以省下的位元組
            "default_roots": self.default_roots(),
        }

    def busy(self) -> bool:
        return self._lock.locked()

    # ---------------- 找重複 ----------------

    def scan_in_background(self, roots: Optional[List[str]] = None) -> bool:
        if self._lock.locked():
            return False
        self.job = DupeJob(kind="scan", running=True, started=time.time())
        threading.Thread(target=self.scan, args=(roots,), daemon=True).start()
        return True

    def scan(self, roots: Optional[List[str]] = None) -> DupeJob:
        """列出範圍內的影片，把 SHA1 和大小都一樣的分成一組，整批換掉上次的結果。"""
        if not self._lock.acquire(blocking=False):
            return self.job
        job = self.job if self.job.running and self.job.kind == "scan" else DupeJob(kind="scan", running=True, started=time.time())
        self.job = job
        try:
            roots = ["/" + r.strip("/") for r in roots or [] if str(r).strip()] or self.default_roots()
            if not roots:
                raise P115Error("還沒有同步任務，請選一個 115 目錄")
            files: Dict[int, dict] = {}
            without_sha1 = 0
            for root in roots:
                self.p115.breaker.check()
                job.current = root
                cid = self.p115.dir_id(root)
                for info in self.p115.iter_changed_files(cid, 0):  # since = 0：全部
                    job.listed += 1
                    if Path(info["name"]).suffix.lower() not in VIDEO_EXTS:
                        continue
                    if not info.get("sha1"):
                        without_sha1 += 1
                        continue
                    files[info["id"]] = info
            if without_sha1 and not files:
                raise P115Error("115 回傳的檔案清單沒有 SHA1，沒辦法比對")
            groups: Dict[Tuple[str, int], List[dict]] = {}
            for info in files.values():
                groups.setdefault((info["sha1"], info["size"]), []).append(info)
            rows = []
            folders: Dict[int, Optional[str]] = {}
            for members in (g for g in groups.values() if len(g) >= 2):
                located = []
                for info in members:
                    job.current = info["name"]
                    path, local = self._locate(info, folders)
                    located.append((info, path, local))
                # 建議保留：本機有 strm 的優先，再來是最早上傳的
                located.sort(key=lambda m: (0 if m[2] else 1, m[0]["mtime"] or 0, m[0]["id"]))
                for i, (info, path, local) in enumerate(located):
                    rows.append((info["id"], info["sha1"], info["size"], info["name"], info["pickcode"],
                                 info["parent_id"], path, local, info["mtime"], int(i == 0)))
            with self.db.lock:
                self.db.conn.execute("DELETE FROM dup_files")
                self.db.conn.executemany(
                    "INSERT INTO dup_files(file_id, sha1, size, name, pickcode, parent_id, path, local, mtime, keep) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)", rows,
                )
                self.db.conn.commit()
            self.db.set_meta(SCAN_META_KEY, json.dumps({"at": int(time.time()), "roots": roots, "files": len(files)},
                                                       ensure_ascii=False))
            log.info("找重複：%s 看了 %s 支影片，%s 支有重複", "、".join(roots), len(files), len(rows))
        except P115Error as exc:
            job.errors.append(str(exc))
            log.warning("找重複失敗：%s", exc)
        except Exception as exc:  # 不能讓背景執行緒默默死掉，網頁要看得到原因
            job.errors.append(f"{type(exc).__name__}: {exc}")
            log.exception("找重複時發生未預期的錯誤")
        finally:
            job.running = False
            job.current = ""
            job.finished = time.time()
            self._lock.release()
        return job

    def _locate(self, info: dict, folders: Dict[int, Optional[str]]) -> Tuple[str, Optional[str]]:
        """115 上的完整路徑，以及本機的 strm（在同步任務裡、而且檔案還在時）。"""
        row = self.db.one("SELECT task, path FROM p115_index WHERE file_id=? AND is_dir=0", (info["id"],))
        if row:
            remote, _, local_root = row["task"].partition("\n")
            path = posixpath.join("/" + remote.strip("/"), posixpath.dirname(row["path"]), info["name"])
            local = Path(local_root).expanduser() / row["path"]
            return path, str(local) if local.is_file() else None
        # 不在同步任務裡：向 115 查資料夾路徑，每個資料夾只查一次
        parent = info["parent_id"]
        if parent not in folders:
            try:
                self.p115.breaker.check()
                folders[parent] = "/" + "/".join(name for _, name in self.p115.dir_ancestors(parent))
            except P115Error as exc:
                log.warning("查不到 115 資料夾 %s 的路徑：%s", parent, exc)
                folders[parent] = None
            delay = getattr(self.strm_sync.cfg, "request_delay", 0) or 0
            if delay:
                time.sleep(delay)
        base = folders[parent]
        return (posixpath.join(base, info["name"]) if base else info["name"]), None

    # ---------------- 清單 ----------------

    def groups(self, query: str = "", offset: int = 0, limit: int = 20) -> dict:
        """一組一組列出來，可以省最多空間的在前面；query 比對檔名和路徑。"""
        where, params = "", []
        if query.strip():
            where = "WHERE sha1 IN (SELECT sha1 FROM dup_files WHERE name LIKE ? OR path LIKE ?)"
            params = [f"%{query.strip()}%"] * 2
        total = self.db.one(f"SELECT COUNT(*) AS c FROM (SELECT 1 FROM dup_files {where} GROUP BY sha1, size)", params)["c"]
        keys = self.db.query(
            f"SELECT sha1, size, COUNT(*) AS n FROM dup_files {where} GROUP BY sha1, size "
            "ORDER BY size * (COUNT(*) - 1) DESC, sha1 LIMIT ? OFFSET ?", (*params, limit, offset),
        )
        items = []
        for k in keys:
            members = self.db.query(
                "SELECT * FROM dup_files WHERE sha1=? AND size=? ORDER BY keep DESC, mtime, file_id", (k["sha1"], k["size"])
            )
            items.append({"sha1": k["sha1"], "size": k["size"], "count": k["n"], "members": [self._member(m) for m in members]})
        return {"items": items, "total": total}

    def _member(self, m) -> dict:
        item = self.db.one("SELECT id, type, name, series_id FROM items WHERE path=?", (m["local"],)) if m["local"] else None
        series = self.db.one("SELECT name FROM items WHERE id=?", (item["series_id"],)) if item and item["series_id"] else None
        watched = self.db.one(
            "SELECT COUNT(*) AS c FROM user_data WHERE item_id=? AND (played=1 OR position_ticks>0 OR is_favorite=1)",
            (item["id"],),
        )["c"] if item else 0
        return {
            "file_id": m["file_id"], "name": m["name"], "path": m["path"], "mtime": m["mtime"], "keep": bool(m["keep"]),
            "local": m["local"], "item": item["name"] if item else None, "series": series["name"] if series else None,
            "watched": bool(watched),
        }

    def recent_deletions(self, limit: int = 50) -> List[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM dup_deleted ORDER BY at DESC, rowid DESC LIMIT ?", (limit,))]

    # ---------------- 刪重複 ----------------

    def plan(self, overrides: Dict[int, bool], sha1: Optional[str] = None, size: Optional[int] = None) -> List[dict]:
        """要刪哪些：預設照建議（不是建議保留的都刪），overrides 可以逐個改（file_id → 要不要刪）。

        給了 sha1、size 時只看那一組。每一組至少要留一份，不然丟 ValueError。
        """
        if sha1:
            rows = self.db.query("SELECT * FROM dup_files WHERE sha1=? AND size=? ORDER BY file_id", (sha1, int(size or 0)))
        else:
            rows = self.db.query("SELECT * FROM dup_files ORDER BY sha1, size, file_id")
        groups: Dict[Tuple[str, int], List] = {}
        for r in rows:
            groups.setdefault((r["sha1"], r["size"]), []).append(r)
        chosen: List[dict] = []
        for members in groups.values():
            picked = [m for m in members if overrides.get(m["file_id"], not m["keep"])]
            if len(picked) == len(members):
                raise ValueError(f"「{members[0]['name']}」這一組每一份都勾了，至少要留一份")
            chosen += [dict(m) for m in picked]
        return chosen

    def delete_in_background(self, plan: List[dict]) -> bool:
        if not plan or self._lock.locked():
            return False
        self.job = DupeJob(kind="delete", running=True, started=time.time(), total=len(plan))
        threading.Thread(target=self.delete, args=(plan,), daemon=True).start()
        return True

    def delete(self, plan: List[dict]) -> DupeJob:
        """把 plan 裡的檔案送進 115 回收站，一次一批；每批成功後處理本機（觀看紀錄、strm、清單）。"""
        if not self._lock.acquire(blocking=False):
            return self.job
        job = self.job if self.job.running and self.job.kind == "delete" else DupeJob(
            kind="delete", running=True, started=time.time(), total=len(plan))
        self.job = job
        planned = {r["file_id"] for r in plan}
        removed: List[str] = []
        try:
            for start in range(0, len(plan), DELETE_BATCH):
                batch = plan[start: start + DELETE_BATCH]
                job.current = batch[0]["name"]
                self.p115.delete_files([r["file_id"] for r in batch])
                removed += self._after_delete(batch, planned)
                job.done += len(batch)
                job.freed += sum(r["size"] for r in batch)
                delay = getattr(self.strm_sync.cfg, "request_delay", 0) or 0
                if delay and start + DELETE_BATCH < len(plan):
                    time.sleep(delay)
            log.info("刪重複：%s 個檔案送進 115 回收站，省下 %.1f GB", job.done, job.freed / 1024 ** 3)
        except P115Error as exc:
            job.errors.append(f"刪到第 {job.done + 1} 個時失敗：{exc}")
            log.warning("刪重複失敗：%s", exc)
        except Exception as exc:
            job.errors.append(f"{type(exc).__name__}: {exc}")
            log.exception("刪重複時發生未預期的錯誤")
        finally:
            job.current = "重新掃描媒體庫" if removed else ""
            try:
                if removed:
                    self.scanner.scan_paths(removed)  # 刪掉的 strm 從媒體庫移除
            finally:
                job.running = False
                job.current = ""
                job.finished = time.time()
                self._lock.release()
        return job

    def _after_delete(self, batch: List[dict], planned: Set[int]) -> List[str]:
        """115 上已經刪了：觀看紀錄轉到保留的那份、刪本機 strm、記下刪了什麼、從清單拿掉。"""
        for r in batch:
            if r["local"]:
                copies = self.db.query(
                    "SELECT file_id, local FROM dup_files WHERE sha1=? AND size=? AND local IS NOT NULL "
                    "ORDER BY keep DESC, mtime", (r["sha1"], r["size"]),
                )
                keeper = next((c for c in copies if c["file_id"] not in planned), None)  # 同一組裡不刪的那份
                if keeper:
                    self._move_user_data(r["local"], keeper["local"])
        removed = self.strm_sync.remove_local([r["file_id"] for r in batch])
        now = int(time.time())
        with self.db.lock:
            c = self.db.conn
            c.executemany("INSERT INTO dup_deleted(file_id, sha1, size, name, path, at) VALUES(?,?,?,?,?,?)",
                          [(r["file_id"], r["sha1"], r["size"], r["name"], r["path"], now) for r in batch])
            c.executemany("DELETE FROM dup_files WHERE file_id=?", [(r["file_id"],) for r in batch])
            # 只剩一份的組不算重複了
            c.execute("DELETE FROM dup_files WHERE sha1 || ':' || size IN "
                      "(SELECT sha1 || ':' || size FROM dup_files GROUP BY sha1, size HAVING COUNT(*) < 2)")
            c.commit()
        return removed

    def _move_user_data(self, src_path: str, dst_path: str) -> None:
        """刪掉的那份上的觀看紀錄（看過、進度、收藏）併到保留的那份，每個使用者分開算。"""
        src = self.db.one("SELECT id FROM items WHERE path=?", (src_path,))
        dst = self.db.one("SELECT id FROM items WHERE path=?", (dst_path,))
        if not src or not dst or src["id"] == dst["id"]:
            return
        for r in self.db.query("SELECT * FROM user_data WHERE item_id=?", (src["id"],)):
            mine = self.db.one("SELECT * FROM user_data WHERE user_id=? AND item_id=?", (r["user_id"], dst["id"]))
            if not mine:
                self.db.execute(
                    "INSERT INTO user_data(user_id, item_id, played, play_count, position_ticks, is_favorite, last_played) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (r["user_id"], dst["id"], r["played"], r["play_count"], r["position_ticks"], r["is_favorite"], r["last_played"]),
                )
                continue
            newer = (r["last_played"] or "") > (mine["last_played"] or "")  # 續播點用最近看的那一份
            self.db.execute(
                "UPDATE user_data SET played=?, play_count=?, position_ticks=?, is_favorite=?, last_played=? "
                "WHERE user_id=? AND item_id=?",
                (max(r["played"] or 0, mine["played"] or 0), (r["play_count"] or 0) + (mine["play_count"] or 0),
                 r["position_ticks"] if newer else mine["position_ticks"], max(r["is_favorite"] or 0, mine["is_favorite"] or 0),
                 max(r["last_played"] or "", mine["last_played"] or "") or None, r["user_id"], dst["id"]),
            )

