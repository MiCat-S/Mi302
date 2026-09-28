"""批量新增媒體庫：看一個上層資料夾底下的子資料夾，猜每個該建成電影還是劇集。

媒體庫資料夾可能在網路磁碟上，所以每個子資料夾只看有限的層數和時間，整體也有時間上限；
沒看完的影片數顯示成「至少 N 部」。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Iterable, List, Optional

from .config import LibraryConfig
from .filetypes import LIBRARY_VIDEO_EXTS
from .scanner import _skip_dir, parse_episode, parse_season_dir

MAX_DEPTH = 5          # 子資料夾往下看幾層（分類／劇名／季／檔案）
FOLDER_SECONDS = 1.5   # 每個子資料夾最多看多久
TOTAL_SECONDS = 12.0   # 整次最多看多久，超過的只看名稱
SAMPLE = 300           # 用前幾部影片判斷類型
MIN_SAMPLE = 3         # 影片少於這個數，改看資料夾名稱
TV_RATIO = 0.3         # 三成以上像劇集的集數，就當劇集

# 名稱判斷：先看電影的字（「动画电影」「剧场版」是電影），再看劇集的字
MOVIE_NAME_RE = re.compile(r"电影|電影|剧场版|劇場版|\bmovies?\b|\bfilms?\b", re.I)
TV_NAME_RE = re.compile(r"剧|劇|番|动漫|動漫|动画|動畫|综艺|綜藝|电视|電視|\btv\b|\bseries\b|\bshows?\b|\banime\b|\bdrama\b", re.I)


def _norm(path: str) -> str:
    return os.path.normpath(os.path.expanduser(path))


def _inside(child: str, parent: str) -> bool:
    return child != parent and child.startswith(parent.rstrip(os.sep) + os.sep)


def used_by(path: str, libraries: Iterable[LibraryConfig]) -> Optional[str]:
    """這個資料夾和現有媒體庫的關係；沒關係回 None。"""
    p = _norm(path)
    for lib in libraries:
        for raw in lib.paths:
            q = _norm(raw)
            if q == p:
                return f"已經是「{lib.name}」的資料夾"
            if _inside(p, q):
                return f"已經包含在「{lib.name}」裡"
            if _inside(q, p):
                return f"裡面有資料夾已經在「{lib.name}」裡"
    return None


def type_by_name(name: str) -> Optional[str]:
    if MOVIE_NAME_RE.search(name):
        return "movies"
    if TV_NAME_RE.search(name):
        return "tvshows"
    return None


def survey(folder: Path, deadline: float) -> dict:
    """數影片、看前幾部像不像劇集。回傳 videos、episodes（取樣裡像劇集的）、sampled、complete。"""
    videos = episodes = 0
    complete = True
    stop = min(deadline, time.monotonic() + FOLDER_SECONDS)
    series: dict = {}  # 資料夾 → 它或上層有 tvshow.nfo（底下的影片都算劇集）
    base_depth = len(folder.parts)
    for root, dirs, files in os.walk(folder, followlinks=True):
        if time.monotonic() > stop:
            complete = False
            break
        here = Path(root)
        depth = len(here.parts) - base_depth
        dirs[:] = sorted(d for d in dirs if not _skip_dir(d)) if depth < MAX_DEPTH else []
        in_series = series[root] = series.get(os.path.dirname(root), False) or "tvshow.nfo" in files
        in_season = depth > 0 and parse_season_dir(here.name) is not None
        for f in files:
            stem, ext = os.path.splitext(f)
            if ext.lower() not in LIBRARY_VIDEO_EXTS:
                continue
            videos += 1
            if videos <= SAMPLE and (in_series or in_season or parse_episode(stem)[1] is not None):
                episodes += 1
    return {"videos": videos, "episodes": episodes, "sampled": min(videos, SAMPLE), "complete": complete}


def guess(name: str, info: Optional[dict]) -> tuple:
    """(類型, 理由)。影片夠多就看內容，不夠就看名稱，都沒有就當電影。"""
    if info and info["sampled"] >= MIN_SAMPLE:
        if info["episodes"] / info["sampled"] >= TV_RATIO:
            return "tvshows", "裡面有集數、季資料夾或 tvshow.nfo"
        return "movies", "裡面的檔案看起來是一部一部的電影"
    by_name = type_by_name(name)
    if by_name:
        return by_name, "依資料夾名稱判斷"
    return "movies", "影片太少，先當電影"


def suggest(parent: str, libraries: List[LibraryConfig]) -> dict:
    """列出 parent 底下的子資料夾，附上建議的名稱、類型、影片數，以及要不要預設勾選。"""
    base = Path(parent).expanduser()
    if not base.is_dir():
        raise ValueError(f"資料夾不存在：{parent}")
    try:
        names = sorted((e.name for e in os.scandir(base) if e.is_dir(follow_symlinks=True) and not _skip_dir(e.name)),
                       key=str.lower)
    except OSError as exc:
        raise ValueError(f"無法讀取：{exc}")
    deadline = time.monotonic() + TOTAL_SECONDS
    folders, partial = [], False
    for name in names:
        path = str(base / name)
        used = used_by(path, libraries)
        info = None
        if time.monotonic() < deadline:
            info = survey(base / name, deadline)
            partial = partial or not info["complete"]
        else:
            partial = True
        ltype, why = guess(name, info)
        folders.append({
            "path": path,
            "name": name,
            "type": ltype,
            "why": why,
            "videos": info["videos"] if info else None,
            "complete": bool(info and info["complete"]),
            "used": used,
            "checked": bool(info and info["videos"]) and not used,
        })
    return {"path": str(base), "folders": folders, "partial": partial}
