"""115 上成對的資料夾：同一部影片一個資料夾名稱帶 {tmdbid=6836}（MoviePilot 整理過的），一個沒有。

依 115 同步紀錄（p115_index）比對目錄樹，不用向 115 請求：同一層底下，去掉 tmdbid 標記後名稱一樣的資料夾
算一組，至少要一個帶標記、一個沒有。找出來之後，網頁把沒有標記的資料夾交給 MoviePilot 整理
（reorganize.folder_plan → preview → execute），TMDB 編號用帶標記那個的、整理到同一層；MoviePilot 照它的
命名設定放進帶標記的資料夾，搬完沒有影片留下的舊資料夾可以順手移到 115 回收站。
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from .browse115 import library_info
from .db import Database
from .filetypes import VIDEO_EXTS
from .strm_sync import remote_root, task_key

# 資料夾名稱裡的 {tmdbid=6836}、[tmdb=6836]、{tmdb-6836}
TMDB_TAG_RE = re.compile(r"\s*[\[{（(]\s*tmdb(?:id)?\s*[=:\-_]\s*(\d+)\s*[\]}）)]", re.I)
SAMPLE = 30  # 看幾支影片在媒體庫裡是電影還是劇集就夠了


def folder_tag(name: str) -> str:
    """資料夾名稱裡的 TMDB 編號，沒有是空字串。"""
    m = TMDB_TAG_RE.search(name)
    return m.group(1) if m else ""


def folder_key(name: str) -> str:
    """比對用的名稱：去掉 tmdbid 標記，全形括號、數字轉半形，空白合併，不分大小寫。"""
    text = unicodedata.normalize("NFKC", TMDB_TAG_RE.sub(" ", name))
    text = re.sub(r"\s*\(\s*", " (", text)
    text = re.sub(r"\s*\)", ")", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def library_type(libraries: Iterable, local: Path) -> str:
    """本機路徑在哪個媒體庫裡：tvshows → tv、movies → movie、不在任何媒體庫 → auto。"""
    for lib in libraries:
        for p in lib.paths:
            try:
                local.relative_to(Path(p).expanduser())
            except ValueError:
                continue
            return {"tvshows": "tv", "movies": "movie"}.get(lib.type, "auto")
    return "auto"


def _is_video(path: str) -> bool:
    """同步紀錄裡的檔案記的是本機路徑：影片是 .strm；沒轉成 strm 的（例如還沒同步到）看副檔名。"""
    ext = posixpath.splitext(path)[1].lower()
    return ext == ".strm" or ext in VIDEO_EXTS


def _video_counts(files: List[Tuple[int, str]]) -> Dict[str, int]:
    """每個資料夾（含子資料夾）底下有幾支影片：資料夾相對路徑 → 數量。"""
    counts: Dict[str, int] = {}
    for _, path in files:
        if not _is_video(path):
            continue
        folder = posixpath.dirname(path)
        while folder:
            counts[folder] = counts.get(folder, 0) + 1
            folder = posixpath.dirname(folder)
    return counts


def _videos_under(files: List[Tuple[int, str]], folder: str, limit: int) -> List[int]:
    prefix = folder + "/"
    out = []
    for fid, path in files:
        if path.startswith(prefix) and _is_video(path):
            out.append(fid)
            if len(out) >= limit:
                break
    return out


def duplicate_folders(db: Database, tasks, libraries: Iterable = ()) -> List[dict]:
    """每一組：{parent, sources: [沒有標記的], targets: [帶標記的], type, videos}；照 115 路徑排。

    type 是 tv／movie／auto：先看這些影片在媒體庫裡是集還是電影，都還沒掃描就看本機資料夾屬於哪種媒體庫。
    targets 通常只有一個；有兩個以上（不同 TMDB 編號）時由網頁選。
    """
    groups: List[dict] = []
    for task in tasks:
        key, root = task_key(task), remote_root(task)
        local_root = Path(task.local).expanduser()
        dirs = [(r["file_id"], r["path"]) for r in
                db.query("SELECT file_id, path FROM p115_index WHERE task=? AND is_dir=1", (key,))]
        by_name: Dict[Tuple[str, str], List[Tuple[int, str, str]]] = {}
        for fid, path in dirs:
            name = posixpath.basename(path)
            norm = folder_key(name)
            if norm:
                by_name.setdefault((posixpath.dirname(path), norm), []).append((fid, path, folder_tag(name)))
        pairs = {k: v for k, v in by_name.items() if any(t for *_, t in v) and not all(t for *_, t in v)}
        if not pairs:
            continue
        files = [(r["file_id"], r["path"]) for r in
                 db.query("SELECT file_id, path FROM p115_index WHERE task=? AND is_dir=0", (key,))]
        counts = _video_counts(files)
        for (parent, _), entries in sorted(pairs.items()):
            entries.sort(key=lambda e: (bool(e[2]), e[1]))

            def view(fid: int, rel: str, tag: str) -> dict:
                return {"cid": fid, "name": posixpath.basename(rel), "path": posixpath.join(root, rel),
                        "tmdbid": tag, "videos": counts.get(rel, 0)}

            sources = [view(*e) for e in entries if not e[2]]
            targets = [view(*e) for e in entries if e[2]]
            sample: List[int] = []
            for _, rel, _ in entries:
                sample += _videos_under(files, rel, SAMPLE - len(sample))
                if len(sample) >= SAMPLE:
                    break
            kinds = {(info or {}).get("type") for info in library_info(db, [task], sample).values()}
            mtype = "tv" if "Episode" in kinds else "movie" if "Movie" in kinds else \
                library_type(libraries, local_root / entries[0][1])
            groups.append({
                "parent": posixpath.join(root, parent) if parent else root, "sources": sources, "targets": targets,
                "type": mtype, "videos": sum(s["videos"] for s in sources),
            })
    return groups
