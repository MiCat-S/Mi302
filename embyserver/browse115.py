"""網頁上瀏覽 115：列出一個資料夾的內容，影片標出在 Mi302 媒體庫裡被認成什麼。

每打開一個資料夾只列一次目錄（檔案多時 115 分頁，每頁 1150 個）；子資料夾的 id 由網頁帶著，不必再查路徑。
對照媒體庫：115 檔案 id → 同步紀錄（p115_index）裡的本機 strm → 媒體庫的項目。
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .db import Database
from .filetypes import VIDEO_EXTS
from .strm_sync import remote_root, task_key

MAX_FILES = 1000  # 一次最多回傳幾個檔案（資料夾全部列出）；更多的用 offset 再要下一批


def _chunks(items: List, size: int = 500) -> Iterable[List]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def library_info(db: Database, tasks, file_ids: Iterable[int]) -> Dict[int, dict]:
    """115 檔案 id → 在 Mi302 的樣子：本機 strm，以及媒體庫裡的項目（還沒掃描到的只有 local）。"""
    roots = {task_key(t): Path(t.local).expanduser() for t in tasks}
    local_of: Dict[str, int] = {}
    ids = list(dict.fromkeys(file_ids))
    for chunk in _chunks(ids):
        marks = ",".join("?" * len(chunk))
        for r in db.query(f"SELECT task, file_id, path FROM p115_index WHERE is_dir=0 AND file_id IN ({marks})", chunk):
            root = roots.get(r["task"])
            if root is not None:
                local_of[str(root / r["path"])] = r["file_id"]
    out: Dict[int, dict] = {fid: {"local": local} for local, fid in local_of.items()}
    paths = list(local_of)
    for chunk in _chunks(paths):
        marks = ",".join("?" * len(chunk))
        for r in db.query(
            "SELECT i.path, i.type, i.name, i.year, i.index_number, i.parent_index_number, i.ep_from, i.provider_ids, "
            f"s.name AS series, s.provider_ids AS series_ids FROM items i LEFT JOIN items s ON s.id=i.series_id "
            f"WHERE i.path IN ({marks})", chunk,
        ):
            out[local_of[r["path"]]].update(
                type=r["type"], name=r["name"], year=r["year"], season=r["parent_index_number"],
                episode=r["index_number"], ep_from=r["ep_from"], series=r["series"],
                provider_ids=r["series_ids"] if r["type"] == "Episode" else r["provider_ids"],
            )
    return out


def sync_root(tasks, path: str) -> Optional[str]:
    """這個 115 路徑在哪個同步任務的目錄裡；不在任何任務裡回傳 None。"""
    for t in tasks:
        root = remote_root(t)
        if path == root or path.startswith(root.rstrip("/") + "/"):
            return root
    return None


def list_folder(p115, db: Database, tasks, cid: int, path: str, offset: int = 0) -> dict:
    """列出 115 資料夾：子資料夾、檔案（影片附上媒體庫資訊）。path 空的時候向 115 查。
    檔案照名稱排，一次給 MAX_FILES 個，從第 offset 個開始（網頁「再載入」用）。"""
    entries = p115.list_dir(cid)
    if not path:
        path = "/" + "/".join(name for _, name in p115.dir_ancestors(cid)) if cid else "/"
    path = "/" + path.strip("/") if path.strip("/") else "/"
    dirs = sorted(({"id": e["id"], "name": e["name"]} for e in entries if e["is_dir"]), key=lambda d: d["name"].lower())
    files = sorted((e for e in entries if not e["is_dir"]), key=lambda e: e["name"].lower())
    offset = max(0, int(offset or 0))
    shown = files[offset:offset + MAX_FILES]
    videos = [e["id"] for e in shown if posixpath.splitext(e["name"])[1].lower() in VIDEO_EXTS]
    lib = library_info(db, tasks, videos)
    return {
        "cid": cid, "path": path, "sync_root": sync_root(tasks, path),
        "dirs": dirs, "total_files": len(files), "offset": offset,
        "files": [
            {"id": e["id"], "name": e["name"], "size": e.get("size") or 0, "mtime": e.get("mtime") or 0,
             "video": e["id"] in lib or posixpath.splitext(e["name"])[1].lower() in VIDEO_EXTS,
             "lib": lib.get(e["id"])}
            for e in shown
        ],
    }
