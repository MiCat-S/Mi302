"""整理 115 網盤：媒體庫裡命名不照 MoviePilot 格式、或集號不對的，整個資料夾交給 MoviePilot 整理；不要的直接刪。

詳細說明見 docs/modules.md 的「embyserver/organize115.py」。
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .db import Database
from .filetypes import VIDEO_EXTS
from .moviepilot import FRONTEND_HINT, MoviePilot, MoviePilotError
from .p115 import P115Error
from .reorganize import ReorgJob, episode_template
from .strm_sync import remote_root, task_key
from .textutil import simplified
from .workers import Workers

log = logging.getLogger(__name__)

UNITS_TTL = 300  # 媒體庫裡有哪些資料夾，幾秒內直接用
LEVELS_META_KEY = "organize_levels"  # 上次檢查時 MoviePilot 的劇集、電影重命名格式各有幾層
PREVIEW_TIMEOUT = 600
SAMPLES = 2  # 一部劇挑幾支影片問 MoviePilot 檔名
MAX_WORKERS = 4  # 同時問 MoviePilot 幾個
MAX_PINNED = 20  # 從瀏覽 115 加進來的資料夾最多留幾個
BATCH_RESULTS = 2000  # 全部整理最多記幾個跳過、失敗的（數量照算，明細只留前面這些）
BATCH_GIVE_UP = 5  # 全部整理時連續幾個預覽出錯（目錄設定對不上、115 讀不到…）就停下
BIG_FOLDER = 300  # 全部整理預設跳過一次要送超過這麼多支影片的（MoviePilot 一次預覽、整理幾千支會吃光記憶體）
HOLD_META_KEY = "organize_hold"  # 標了「先不整理」的資料夾（115 路徑）
MP_DOWN_WAIT = 900  # 全部整理時 MoviePilot 連不上，最多等幾秒
MP_DOWN_POLL = 15  # 等的時候幾秒問一次
MP_COOLDOWN = 60  # MoviePilot 恢復、或途中斷線之後，再等幾秒才送下一個（它可能還在背景做剛才那一個）
# 全部整理的一個資料夾做完的結果：OK；ERROR＝預覽出錯（連續太多就停）；RETRY＝MoviePilot 連不上、什麼都還沒做，
# 等它回來再做一次；DOWN＝MoviePilot 途中斷線或等太久，它可能還在背景做，已經記下來，不重送
OK, ERROR, RETRY, DOWN = "ok", "error", "retry", "down"
DOWN_WORDS = {"offline": "連不上", "dropped": "斷線", "timeout": "等太久沒有回應"}
RECOMMEND_FILES = 50  # 請 MoviePilot 推薦集數定位時最多給幾個檔名
P115_NEEDED = "要先登入 115（掃碼或貼上 cookie）才能整理"


class OrganizeError(Exception):
    pass


def _mp_trouble(exc: Optional[BaseException]) -> str:
    """錯誤是 MoviePilot 連線出事造成的（OrganizeError 是在接住 MoviePilotError 時丟的）就回傳它的 kind。"""
    while exc is not None:
        if isinstance(exc, MoviePilotError):
            return exc.kind
        exc = exc.__cause__ or exc.__context__
    return ""


def _biggest(u) -> int:
    """一次要送給 MoviePilot 的影片最多幾支（每一部分各送一次；不知道的算 0）。"""
    return max([p.videos or 0 for p in u.parts] or [u.videos or 0])


@dataclass
class Target:
    """整理到哪裡、MoviePilot 會用哪一種覆蓋模式。"""

    path: Optional[str]  # 送給 MoviePilot 的 target_path；None = 讓它照自己的目錄設定挑
    overwrite: str = "never"  # 那個媒體庫目錄的覆蓋模式：never／size／always／latest
    note: str = ""


OVERWRITE_RISK = ["", "never", "size", "always", "latest"]  # 越後面越會刪東西
OVERWRITE_NAMES = {"size": "按大小覆蓋", "always": "覆蓋", "latest": "保留最新"}
MOVIE_TYPES, TV_TYPES = {"电影", "movie"}, {"电视剧", "tv"}


def _dir_path(value) -> str:
    return "/" + str(value or "").strip().strip("/")


def _inside(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root.rstrip("/") + "/")


def _type_ok(d: dict, kind: str) -> bool:
    """這個目錄設定收不收這種資料夾（沒設媒體類型的都收；瀏覽 115 加進來的不知道是什麼，也都收）。"""
    t = str(d.get("media_type") or "").strip().lower()
    if kind == "folder" or t in ("", "none"):
        return True
    return kind == "series" if t in TV_TYPES else kind in ("movie", "movie_file") if t in MOVIE_TYPES else False


def _overwrite(dirs: List[dict], library_path: str) -> str:
    """送 target_path 時 MoviePilot 拿它去對媒體庫目錄：對上的（有開整理、存儲是 115）用那一項的覆蓋模式，
    都對不上用「不覆蓋」。好幾項對得上時它照媒體類型挑一項，Mi302 不知道是哪一項，取最會刪東西的那個。"""
    modes = [str(d.get("overwrite_mode") or "") for d in dirs
             if d.get("monitor_type") and d.get("library_storage") == "u115" and _dir_path(d.get("library_path")) == library_path]
    return max(modes, key=lambda m: OVERWRITE_RISK.index(m) if m in OVERWRITE_RISK else 0) if modes else "never"


def _levels(template: str) -> int:
    """重命名格式有幾層（劇集預設三層：劇名資料夾／季資料夾／檔名；電影兩層）。"""
    return template.count("/") + 1 if template else 0


def _stem(name: str) -> str:
    return posixpath.splitext(name)[0]


def _named_after(title: str, year: Optional[int], folder: str) -> bool:
    """資料夾是不是以這部片命名的（例如「H-画江湖之天罡-2023-[tmdb=1221210]」）：名稱有片名，還有年份或 tmdb 標記。
    分類資料夾（动画电影）沒有；只看片名的話「Up」這種短片名會對到「Upcoming」。"""
    name, title = posixpath.basename(folder.rstrip("/")).casefold(), (title or "").strip().casefold()
    return bool(title) and title in name and ((bool(year) and str(year) in name) or "tmdb" in name)


NAME_WORDS = re.compile(r"[\s._]+")


def _same_name(a: str, b: str) -> bool:
    """名稱一樣：不分大小寫、不管詞的順序。MoviePilot 解析檔名時把「DV HQ」這類效果倒過來排，它自己取的名稱
    再問一次會變成「HQ DV」、再問又變回來，只差順序的不算不一樣。"""
    def words(s: str) -> List[str]:
        return sorted(w for w in NAME_WORDS.split(s.casefold()) if w)
    return words(a) == words(b)


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
    others: int = 0  # movie_file：同一個資料夾（含子資料夾）裡的其他影片

    @property
    def parent(self) -> str:
        return posixpath.dirname(self.path)

    @property
    def sig(self) -> str:
        return hashlib.sha1(json.dumps([self.name, self.samples, self.videos, self.loose]).encode()).hexdigest()[:16]

    def cleanup_folders(self) -> List[dict]:
        """整理完要看要不要移到回收站的舊資料夾（沒有影片留下才移），裡面的先：自己的資料夾；放在以片名命名的
        資料夾裡的（沒有自己資料夾的電影、或整個資料夾又套在同名資料夾裡），那個資料夾也算。id 用字串給網頁。"""
        out = [] if self.kind == "movie_file" else [{"cid": str(self.cid), "path": self.path}]
        holder, holder_cid = self.parent, self.cid if self.kind == "movie_file" else self.parent_cid
        if holder_cid and _named_after(self.title, self.year, holder):
            out.append({"cid": str(holder_cid), "path": holder})
        return out

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
            "ep_guessed": self.ep_guessed, "ep_unknown": self.ep_unknown, "cleanup": self.cleanup_folders(),
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
                1, len(here), [posixpath.join(root, strm_rel)], tree.children.get(folder, []), local=str(Path(local) / strm_rel),
                file_id=fid, others=max(tree.total.get(folder, 0) - 1, 0), parts=[Part("file", "這支影片", cid, posixpath.join(root, folder), str(Path(local) / folder), 1,
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
        # 資料夾名稱有片名：是這部電影的資料夾，只是裡面（或子資料夾裡）還有別的影片（常是 115 加了「(1)」的重複檔案、
        # 另一個版本）；名稱沒有片名的是分類資料夾，這部電影真的沒有自己的資料夾
        own = unit.others and _named_after(unit.title, unit.year, unit.parent)
        if movie_levels >= 2:
            unit.reasons.append(f"資料夾裡還有另外 {unit.others} 支影片（MoviePilot 會給每部電影自己的資料夾；"
                                "同一部的重複檔案可以先到「整理 → 重複檔案」清掉）" if own
                                else "沒有自己的資料夾（MoviePilot 會放進自己的資料夾）")
    elif unit.mp_name and not _same_name(unit.mp_name, unit.name):
        unit.reasons.append("資料夾名稱和 MoviePilot 的不一樣")
        if unit.mp_name in unit.siblings:
            unit.merge_into = {"name": unit.mp_name, "path": posixpath.join(unit.parent, unit.mp_name)}
    if unit.kind in ("movie", "series") and _named_after(unit.title, unit.year, unit.parent):
        unit.reasons.append(f"套在另一個以片名命名的資料夾「{posixpath.basename(unit.parent)}」裡（MoviePilot 會直接放在分類資料夾底下）")
    if unit.kind == "series" and tv_levels >= 3 and unit.loose:
        unit.reasons.append(f"{unit.loose} 支影片直接放在資料夾裡（MoviePilot 會放進季資料夾）")
    if any(not _same_name(_stem(f[0]), _stem(f[1])) for f in unit.mp_files):
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


@dataclass
class BatchJob:
    """全部整理：一個一個預覽，沒問題的直接整理，有問題的跳過記下來。"""

    running: bool = False
    started: float = 0.0
    finished: float = 0.0
    total: int = 0  # 這次要處理的資料夾
    done: int = 0  # 處理過的（不管結果）
    organized: int = 0  # 整理了的資料夾
    files: int = 0  # MoviePilot 整理好的檔案
    queued: int = 0  # 放進它背景佇列的檔案（結果在它的整理記錄）
    renamed: int = 0  # Mi302 整理助手改好名字的資料夾
    nothing: int = 0  # 預覽後沒有要整理的（都已經照格式命名，或都不送）
    skipped: int = 0  # 有問題跳過的，要人看
    failed: int = 0  # 執行時有檔案失敗的資料夾
    held: int = 0  # 這次不做的：標了「先不整理」的、一次要送的影片超過 max_videos 的（不算在 total 裡）
    max_videos: int = 0
    mp_down: int = 0  # 中途 MoviePilot 連不上、斷線、等太久幾次（每次都等它恢復才繼續）
    current: str = ""
    stopping: bool = False
    stopped: bool = False
    synced: str = ""  # 之後的增量同步：started / busy
    error: str = ""
    target: str = ""
    results: List[dict] = field(default_factory=list)  # 跳過、失敗的：id、name、path、kind（skipped／failed）、why

    def as_dict(self, results: bool = True) -> dict:
        d = asdict(self)
        if not results:
            d["results"] = []
        d["result_count"] = len(self.results)
        d["results_total"] = self.skipped + self.failed  # 明細最多留 BATCH_RESULTS 個，這是全部的
        d["truncated"] = d["results_total"] > len(self.results)
        return d


class Organizer:
    def __init__(self, db: Database, strm_sync, moviepilot: MoviePilot, reorganizer, scanner=None):
        self.db = db
        self.strm_sync = strm_sync
        self.mp = moviepilot
        self.reorg = reorganizer
        self.scanner = scanner
        self._stamp: tuple = ()  # 上次算清單時媒體庫掃描、115 同步完成的時間；變了就重算
        self.job = CheckJob()
        self.batch = BatchJob()
        self._lock = threading.Lock()  # 同時只跑一個檢查
        # 檢查和全部整理不同時跑（兩邊都會改同一批資料夾、同時問 MoviePilot）；這把鎖只包「看對方、標記自己」那一段
        self._starting = threading.Lock()
        self.workers = Workers()  # 檢查、全部整理；程式結束時停在兩個資料夾之間
        self._stop = self.workers.stop
        self._units: Dict[str, Unit] = {}
        self._order: List[str] = []
        self._units_at = 0.0
        self._pinned: Dict[str, Unit] = {}  # 從瀏覽 115 加進來的資料夾（新的在前面）
        try:
            self._held: Set[str] = set(json.loads(db.get_meta(HOLD_META_KEY) or "[]"))
        except (ValueError, TypeError):
            self._held = set()
        try:
            tv, movie = json.loads(db.get_meta(LEVELS_META_KEY) or "[3, 2]")
            self._levels: Tuple[int, int] = (int(tv), int(movie))
        except (ValueError, TypeError):
            self._levels = (3, 2)

    @property
    def p115(self):
        return self.strm_sync.p115

    def stop(self) -> None:
        self._stop.set()

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
        with self._starting:
            if self.batch.running:
                raise OrganizeError("正在全部整理，等它做完再檢查")
            if not self._lock.acquire(blocking=False):
                return False
            self.job = CheckJob(running=True, started=time.time())
        self.workers.start(self._check, refresh)
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
                    if not job.error and self._stop.is_set():
                        job.error = "程式要結束，檢查中途停下（沒問的下次再問）"
                    if job.error:
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

    def _matching(self, units: List[Unit], q: str, kind: str) -> List[Unit]:
        """清單上看得到的（要整理的），照搜尋和種類篩選；不含瀏覽 115 釘上來的。"""
        text = q.strip().casefold()

        def wanted(u: Unit) -> bool:
            if kind == "series" and u.kind != "series" or kind == "movie" and u.kind == "series":
                return False
            if kind == "held" and u.path not in self._held:
                return False
            if kind == "episodes" and not (u.ep_guessed or u.ep_unknown):
                return False
            return not text or text in u.name.casefold() or text in (u.title or "").casefold() or text in u.path.casefold()

        found = [u for u in units if u.listed and wanted(u) and u.id not in self._pinned]
        return sorted(found, key=lambda u: u.path in self._held)  # 先不整理的排在後面

    def list(self, q: str = "", kind: str = "", offset: int = 0, limit: int = 50) -> dict:
        units = self.units()
        found = [u for u in units if u.listed]
        counts = {"series": sum(1 for u in found if u.kind == "series"), "movie": sum(1 for u in found if u.kind != "series"),
                  "episodes": sum(1 for u in found if u.ep_guessed or u.ep_unknown),
                  "held": sum(1 for u in found if u.path in self._held)}
        shown = self._matching(units, q, kind)
        return {
            "job": self.job.as_dict(), "batch": self.batch.as_dict(results=False), "total": len(shown), "folders": len(units),
            "unchecked": sum(1 for u in units if not u.checked), "counts": counts,
            "pinned": [self._view(u) for u in self._pinned.values()],
            "items": [self._view(u) for u in shown[offset:offset + limit]],
        }

    def _view(self, u: Unit) -> dict:
        return {**u.view(), "held": u.path in self._held}

    def hold(self, unit_id: str, on: bool) -> dict:
        """「先不整理」：全部整理時跳過，排到清單後面；單獨整理還是可以。記 115 路徑，清單重算也還在。"""
        unit = self.unit(unit_id)
        with self._starting:
            held = set(self._held)
            (held.add if on else held.discard)(unit.path)
            self.db.set_meta(HOLD_META_KEY, json.dumps(sorted(held), ensure_ascii=False))
            self._held = held
        return self._view(unit)

    def nonstandard_series(self) -> Set[int]:
        """要整理的劇（問過 MoviePilot 不一樣的，或集號不對的）。"""
        return {u.item_id for u in self.units() if u.kind == "series" and u.listed}

    # ---------------- 預覽 ----------------

    def _target(self, unit: Unit, target: str, target_path: str) -> Target:
        """整理到哪裡。auto：MoviePilot 自己挑目錄時只看下載目錄，媒體庫裡的資料夾對不上（預覽只會說「整理任务处理失败」）；
        類型、類別資料夾又不加，所以已經在它的媒體庫目錄（存儲是 115）裡的，留在現在的分類資料夾（和「同一層」一樣）。
        不在任何媒體庫目錄裡的才讓它自己挑，挑不出來就說清楚。"""
        try:
            dirs = self.mp.library_dirs()
        except MoviePilotError as exc:
            if target == "auto":
                raise OrganizeError(f"讀不到 MoviePilot 的目錄設定：{exc}")
            dirs = []  # 自己指定的位置用不到目錄設定，只是看不到覆蓋模式；對不上的它用「不覆蓋」
        if target in ("path", "parent"):
            dest = _dir_path(target_path) if target == "path" else self._same_level(unit)
            if dest == "/":
                raise OrganizeError("請填要整理到哪個 115 資料夾")
            return Target(dest, _overwrite(dirs, dest))
        where = unit.path if unit.kind != "movie_file" else unit.parent
        libs = [d for d in dirs if d.get("library_storage") == "u115" and d.get("library_path")
                and _inside(where, _dir_path(d.get("library_path"))) and _type_ok(d, unit.kind)]
        if libs:
            d = max(libs, key=lambda d: len(_dir_path(d.get("library_path"))))  # 最裡面的那個
            dest = self._same_level(unit)
            return Target(dest, _overwrite(dirs, dest),
                          f"已經在 MoviePilot 的媒體庫目錄「{d.get('name') or _dir_path(d.get('library_path'))}」裡：留在 {dest}，"
                          "只改資料夾和檔名（不加類型、類別資料夾）")
        name = posixpath.basename(where.rstrip("/"))
        item = {"storage": "u115", "type": "dir", "path": where.rstrip("/") + "/", "name": name, "basename": name,
                "fileid": str(unit.cid)}
        try:
            found = self.mp.transfer_target(item)
        except MoviePilotError as exc:
            raise OrganizeError(f"問 MoviePilot 會整理到哪裡時失敗：{exc}")
        if not found:
            raise OrganizeError(f"MoviePilot 的目錄設定裡，沒有哪個媒體庫目錄（存儲是 115 網盤）包含 {where}，它自己也挑不出目錄"
                                "（它只認來源在「下載目錄」底下、下載目錄存儲也是 115、整理方式不是「不整理」的那幾項）。"
                                "到 MoviePilot 的目錄設定加一個包含這裡的媒體庫目錄，或把「整理到」改成「同一層」或指定的 115 資料夾")
        path = _dir_path(found.get("target_path"))
        return Target(None, _overwrite(dirs, path), f"MoviePilot 照它的目錄設定整理到媒體庫 {path}（不加類型、類別資料夾）")

    def _same_level(self, unit: Unit) -> str:
        """「同一層」：和現在的資料夾放在一起。放在以片名命名的資料夾裡的（例如沒有自己資料夾的電影在
        「H-画江湖之天罡-2023-[tmdb=…]」裡），再往上到那個資料夾的上一層，不然 MoviePilot 會把新的電影資料夾建在舊的裡面；
        不超出同步目錄。"""
        roots = {r.rstrip("/") or "/" for r in self._roots()}
        folder = unit.parent
        while folder not in ("", "/") and folder.rstrip("/") not in roots and _named_after(unit.title, unit.year, folder):
            folder = posixpath.dirname(folder.rstrip("/"))
        return folder or "/"

    def preview(self, unit_id: str, overrides: Dict[str, dict], target: str = "", target_path: str = "",
                scrape: bool = False) -> dict:
        """請 MoviePilot 只算不做，一個部分一次。overrides：{部分: {type, tmdbid, season, format}}，沒給的讓它自己認。
        target：auto（照 MoviePilot 的目錄設定，預設）、parent（同一層）、path（target_path）。"""
        return self._preview(self.unit(unit_id), overrides, target, target_path, scrape)

    def _preview(self, unit: Unit, overrides: Dict[str, dict], target: str, target_path: str, scrape: bool) -> dict:
        """preview 的本體（全部整理直接拿 Unit 呼叫，中途清單更新也不影響）。回傳的 review 是要人看一下的原因：
        認成別部片（資料夾名裡的 TMDB 編號錯了）、有檔案 MoviePilot 不會整理、有要看一下的、認成的季和媒體庫不一樣、
        查不到整理紀錄或目標資料夾；全部整理時跳過這些。"""
        try:
            self.mp.check_transfer_preview()  # 舊版 MoviePilot 會把預覽當成真的整理
        except MoviePilotError as exc:
            raise OrganizeError(str(exc))
        target = target or "auto"
        tgt = self._target(unit, target, target_path)
        dest = tgt.path
        roots = self._roots()
        items: List[dict] = []
        batches: List[dict] = []
        notes: List[str] = [tgt.note] if tgt.note else []
        review: List[str] = []
        parts: List[dict] = []
        listings: Dict[str, List[dict]] = {}
        identity: List[str] = []  # 認成別部片：最根本的原因，放在 review 最前面
        for part in unit.parts:
            o = _override(overrides.get(part.key) or {})
            try:
                fileitems, single = self._fileitems(unit, part, listings)
            except P115Error as exc:
                raise OrganizeError(f"讀不到 115 上的檔案：{exc}")
            if not fileitems:
                notes.append(f"「{part.label}」在 115 上找不到影片，這次不送（先同步一次）")
                continue
            results = self._preview_results(unit, part, o, fileitems, single, dest, scrape, listings, notes)
            views = [_view(r, part, roots, tgt.overwrite) for r in results]
            _mark_duplicates(views)
            self._mark_existing(views, part, tgt.overwrite, listings, notes, review)
            self._mark_no_episode(views, part, listings)
            recognized = _recognized(views)
            _check_recognized(unit, part, o, views, recognized, notes, review, identity)
            skipped = Counter(v["skip"] for v in views if v["skip"])
            if skipped:
                notes.append(f"「{part.label}」" + "、".join(f"{n} 個{SKIP_LABELS[k]}" for k, n in skipped.items()) + "，這些不送")
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
                # MoviePilot 整理過的（紀錄還在）：和它的網頁一樣重新整理，不然執行時會被當成「已整理過」跳過
                try:
                    history = self.mp.transfer_history(fileitems)
                except MoviePilotError as exc:
                    if exc.kind:
                        raise OrganizeError(f"問 MoviePilot 的整理紀錄時失敗：{exc}")
                    history = 0
                    notes.append(f"「{part.label}」查不到 MoviePilot 的整理紀錄（{exc}），整理過的可能會被它跳過")
                    review.append(f"「{part.label}」查不到 MoviePilot 的整理紀錄")
                if history:
                    notes.append(f"「{part.label}」MoviePilot 有 {history} 條成功整理的紀錄，執行時會和它的網頁一樣重新整理："
                                 "清掉舊紀錄；舊紀錄是複製、連結整理的，舊的目標檔案也會刪掉")
                batches.append({"fileitems": fileitems, "single": single and len(fileitems) == 1, "season": o["season"],
                                "tmdbid": o["tmdbid"], "type_name": o["type_name"], "episode_format": o["format"],
                                "count": ok, "label": part.label, "local": part.local, "reorganize": bool(history)})
        batches = self._rename_batches(unit, items, listings, notes, batches)
        folders = sorted({posixpath.dirname(v["target"]) for v in items if v["ok"] and v["target"]})
        token = self._remember(unit, batches, tgt, scrape) if batches else None
        summary = _summary(items, review)
        review[:0] = identity
        return {"token": token, "items": items, "notes": notes, "folders": folders, "parts": parts, "target": target,
                "summary": summary, "review": review}

    def _preview_results(self, unit: Unit, part: Part, o: dict, fileitems: List[dict], single: bool, dest: Optional[str],
                         scrape: bool, listings: Dict[str, List[dict]], notes: List[str]) -> List[dict]:
        """這一部分每個檔案整理後叫什麼、放哪裡（格式和 MoviePilot 的整理預覽一樣）。整理到哪裡已經定了、外掛會算名字時，
        請 Mi302 整理助手照 MoviePilot 的規則算（同一批函式，名字和它整理的一樣；幾百集幾秒，不必跑它整套整理預覽）；
        不然、或外掛出錯，跑它的整理預覽。"""
        if dest and self.mp.naming_plugin_ready():
            try:
                files = self._part_files(part, listings)
                results = self.mp.plugin_names(files, o["tmdbid"] or None, o["season"], o["format"] or None,
                                               o["type_name"], timeout=PREVIEW_TIMEOUT)
                if NAMED_NOTE not in notes:
                    notes.append(NAMED_NOTE)
                base = dest.rstrip("/")
                return [dict(r, target=f"{base}/{r['target']}" if r.get("target") else None) for r in results]
            except (MoviePilotError, P115Error) as exc:
                notes.append(f"「{part.label}」Mi302 整理助手算名字失敗（{exc}），改用 MoviePilot 的整理預覽")
        try:
            return self.mp.transfer(fileitems, o["tmdbid"] or None, o["season"], o["format"] or None, scrape, dest,
                                    preview=True, mtype=o["type_name"], timeout=PREVIEW_TIMEOUT, single=single)
        except MoviePilotError as exc:
            raise OrganizeError(f"MoviePilot 預覽失敗：{exc}")

    def _part_files(self, part: Part, listings: Dict[str, List[dict]]) -> List[dict]:
        """這一部分要算名字的檔案，和送給 MoviePilot 整理的一樣：整個資料夾的話連子資料夾裡的都算；直接放著的影片
        只算這幾支，和旁邊以它的檔名開頭的字幕、音軌（它整理一支影片時會一起帶走）。不是影片、字幕、音軌的外掛會略過。"""
        if part.loose:
            stems = set(part.stems)
            return [self._name_item(part.remote, e) for e in self._listing(part.remote, part.cid, listings)
                    if not e["is_dir"] and (_stem(e["name"]) in stems and _is_video(e["name"])
                                            or any(e["name"].startswith(s + ".") for s in stems))]
        out: List[dict] = []
        todo = [(part.remote.rstrip("/"), part.cid)]
        while todo:
            folder, cid = todo.pop()
            for e in self._listing(folder, cid, listings):
                if e["is_dir"]:
                    todo.append((f"{folder}/{e['name']}", int(e["id"])))
                else:
                    out.append(self._name_item(folder, e))
        return out

    @staticmethod
    def _name_item(folder: str, e: dict) -> dict:
        return {"path": f"{folder.rstrip('/')}/{e['name']}", "fileid": str(e["id"]), "size": int(e.get("size") or 0)}

    def _rename_batches(self, unit: Unit, items: List[dict], listings: Dict[str, List[dict]], notes: List[str],
                        batches: List[dict]) -> List[dict]:
        """結構已經對、只是名字不對：換成一批交給 Mi302 整理助手直接改名（用 MoviePilot 的 115 授權，一個檔案一個請求）；
        不是的話（或沒裝外掛）照舊回傳 batches，交給 MoviePilot 整理。"""
        if not batches or not self.mp.rename_plugin_ready():
            return batches
        try:
            renames = self._rename_plan(unit, items, listings)
        except P115Error as exc:
            notes.append(f"查 115 上的檔名時失敗（{exc}），照舊交給 MoviePilot 整理")
            return batches
        if not renames:
            return batches
        files = sum(1 for r in renames if r["type"] == "file")
        notes.insert(0, f"只需要改名：交給 MoviePilot 的「Mi302 整理助手」直接改 {files} 個檔案、"
                        f"{len(renames) - files} 個資料夾的名字，不走整理流程，快很多")
        return [{"mode": "rename", "renames": renames, "count": sum(1 for v in items if v["ok"]), "label": unit.name,
                 "local": None}]

    @staticmethod
    def _folder_map(unit: Unit, ok: List[dict]) -> Optional[Dict[str, str]]:
        """每支檔案的舊資料夾 → 新資料夾（每一層都對）；層數不一樣、同一個資料夾要分到不同地方、兩個要併成一個時回傳 None。"""
        base = unit.parent
        folders: Dict[str, str] = {}
        for v in ok:
            src, dst = posixpath.dirname(v["source"].rstrip("/")), posixpath.dirname(v["target"].rstrip("/"))
            if not (_inside(src, unit.path) and _inside(dst, base)) or dst == base:
                return None
            rs, rd = src[len(base):].strip("/").split("/"), dst[len(base):].strip("/").split("/")
            if len(rs) != len(rd):
                return None  # 要加或拿掉一層資料夾（例如影片直接放在劇的資料夾裡、要放進季資料夾）
            for k in range(1, len(rs) + 1):
                old, new = base.rstrip("/") + "/" + "/".join(rs[:k]), base.rstrip("/") + "/" + "/".join(rd[:k])
                if folders.setdefault(old, new) != new:
                    return None  # 同一個資料夾裡的檔案要分到不同的地方
        return folders if len(set(folders.values())) == len(folders) else None  # 兩個變成同一個：要合併，得用整理

    def _rename_plan(self, unit: Unit, views: List[dict], listings: Dict[str, List[dict]]) -> Optional[List[dict]]:
        """這個資料夾是不是只需要原地改名：每支要送的檔案，新位置和舊位置的資料夾層數一樣，每個舊資料夾對到唯一一個
        新名字，新名字也沒有和旁邊已經有的資料夾、檔案撞名。是的話回傳要改的清單（先檔案，再裡面的資料夾，最後外面的），
        給 Mi302 整理助手照順序改；要搬位置、加一層季資料夾、併進別的資料夾的，回傳 None，照舊交給 MoviePilot 整理。"""
        ok = [v for v in views if v["ok"]]
        base = unit.parent
        if unit.kind == "movie_file" or not ok or unit.path == base:
            return None
        folders = self._folder_map(unit, ok)
        if folders is None:
            return None
        ids: Dict[str, int] = {unit.path: unit.cid}

        def entries(folder: str) -> List[dict]:
            if folder not in ids:
                parent = posixpath.dirname(folder)
                hit = next((e for e in entries(parent) if e["is_dir"] and e["name"] == posixpath.basename(folder)), None)
                if hit is None:
                    raise P115Error(f"115 上找不到 {folder}")
                ids[folder] = hit["id"]
            return self._listing(folder, ids[folder], listings)

        ids[base] = unit.parent_cid
        renames: List[dict] = []
        for folder in sorted({posixpath.dirname(v["source"].rstrip("/")) for v in ok}):
            here = {e["name"]: e for e in entries(folder) if not e["is_dir"]}
            moving = {posixpath.basename(v["source"]): posixpath.basename(v["target"]) for v in ok
                      if posixpath.dirname(v["source"].rstrip("/")) == folder}
            final = [n for n in here if n not in moving] + list(moving.values())
            if len(final) != len(set(final)) or any(n not in here for n in moving):
                return None  # 改名後會和留在這裡的檔案撞名，或 115 上已經沒有這個檔案
            if any(new != old and new in moving for old, new in moving.items()):
                return None  # 新名字是同一批另一個檔案現在的名字（互換、連鎖）：外掛一個一個改，改的時候那個檔案還在
            renames += [{"fileid": str(here[old]["id"]), "name": new, "old": old, "path": posixpath.join(folder, old),
                         "type": "file"} for old, new in sorted(moving.items()) if old != new]
        for old, new in sorted(folders.items(), key=lambda kv: -kv[0].count("/")):
            if posixpath.basename(old) == posixpath.basename(new):
                continue
            siblings = {e["name"] for e in entries(posixpath.dirname(old)) if e["name"] != posixpath.basename(old)}
            if posixpath.basename(new) in siblings:
                return None  # 旁邊已經有同名的：要併進去，得用整理
            entries(old)  # 順便確定它的 id
            renames.append({"fileid": str(ids[old]), "name": posixpath.basename(new), "old": posixpath.basename(old),
                            "path": old.rstrip("/") + "/", "type": "dir"})
        return renames or None

    def _mark_existing(self, views: List[dict], part: Part, overwrite: str, listings: Dict[str, List[dict]],
                       notes: List[str], review: List[str]) -> None:
        """MoviePilot 預覽不看目標是不是已經有同名檔案，真的整理時才發現：覆蓋模式「不覆蓋」（或沒設）時失敗
        「媒体库存在同名文件」；之前失敗過的，它還會照上次的計畫在背景重試。所以到 115 上看目標資料夾：
        有同名檔案、不會覆蓋的不送（多半是 115 加了「(1)」的重複檔案），附上兩邊的大小和這支的 id，
        網頁上可以直接「刪掉這支」；會覆蓋的提醒。"""
        folders: Dict[str, List[dict]] = {}
        for v in views:
            if v["ok"] and v["target"]:
                folders.setdefault(posixpath.dirname(v["target"]), []).append(v)
        for folder, vs in folders.items():
            try:
                cid = self.p115.dir_id(folder)
                existing = {e["name"]: e for e in self._listing(folder, cid, listings) if not e["is_dir"]}
            except P115Error as exc:
                if "找不到目錄" not in str(exc):  # 還沒有這個資料夾：當然沒有同名檔案
                    notes.append(f"看不到 {folder} 裡有沒有同名的檔案（{exc}），整理時已經有的可能會失敗")
                    review.append(f"看不到 {folder} 裡有沒有同名的檔案")
                continue
            for v in vs:
                there = existing.get(posixpath.basename(v["target"]))
                if not there:
                    continue
                if overwrite in ("", "never"):
                    v.update(ok=False, skip="exists", exists_size=int(there.get("size") or 0),
                             message="目標已經有同名的檔案，MoviePilot 不會整理（覆蓋模式是「不覆蓋」）。這支多半是重複的："
                                     "比一下大小，確認後按「刪掉這支」移到 115 回收站")
                    v.update(self._source_file(v["source"], part, listings))
                else:
                    v["warnings"].append(f"目標已經有同名的檔案，MoviePilot 的覆蓋模式是「{OVERWRITE_NAMES.get(overwrite, overwrite)}」，"
                                         + ("會用這支蓋掉它" if overwrite == "always" else "會照這個模式留下其中一個"))

    def _mark_no_episode(self, views: List[dict], part: Part, listings: Dict[str, List[dict]]) -> None:
        """認不出集號的（番外、合集、預告片…）MoviePilot 不會整理：附上這支的 id、所在資料夾和大小，網頁上可以只刪這一支。"""
        for v in views:
            if not v["ok"] and not v["skip"] and NO_EPISODE in v["message"]:
                v.update(self._source_file(v["source"], part, listings))

    def _source_file(self, source: str, part: Part, listings: Dict[str, List[dict]]) -> dict:
        """預覽裡一個來源檔案在 115 上的 id、所在資料夾 id 和大小（給網頁上「刪掉這支」）；找不到回空的。"""
        folder = posixpath.dirname(source.rstrip("/"))
        try:
            cid = part.cid if folder == part.remote.rstrip("/") else self.p115.dir_id(folder)
            entry = next((e for e in self._listing(folder, cid, listings)
                          if not e["is_dir"] and e["name"] == posixpath.basename(source)), None)
        except P115Error:
            return {}
        if not entry:
            return {}
        return {"file_id": str(entry["id"]), "parent_cid": str(cid), "size": int(entry.get("size") or 0)}

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

    def _remember(self, unit: Unit, batches: List[dict], tgt: Target, scrape: bool) -> str:
        payload = {"plan_id": f"o{unit.id}", "mode": "organize", "title": unit.path,
                   "cid": unit.cid if unit.kind != "movie_file" else None,
                   "cids": [int(c["cid"]) for c in unit.cleanup_folders()], "target_path": tgt.path, "scrape": bool(scrape),
                   "batches": batches}
        token = hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]
        self.reorg.remember_preview(token, payload)
        return token

    # ---- 全部整理 ----

    def organize_all(self, q: str, kind: str, target: str, target_path: str, cleanup: bool,
                     max_videos: int = BIG_FOLDER) -> dict:
        """清單上符合搜尋、種類的（加上瀏覽 115 釘上來的）全部整理：在背景一個一個請 MoviePilot 預覽，沒問題的直接照預覽
        整理，有問題的（見 _preview 的 review）跳過記下來；最後同步一次。和單獨整理共用一把鎖，同時只有一批在動 115；
        「問 MoviePilot 檢查」在跑時不開始。標了「先不整理」的（只看它們時除外）、一次要送的影片超過 max_videos 支的
        （0 = 不限）這次不做。"""
        if not self.p115.logged_in:
            raise OrganizeError(P115_NEEDED)  # 不然每個資料夾都列不出來，連續出錯才停
        with self._starting:
            if self.batch.running:
                raise OrganizeError("已經在全部整理了")
            if self._lock.locked():
                raise OrganizeError("正在問 MoviePilot 檢查，等它問完再全部整理")
            units = list(self._pinned.values()) + self._matching(self.units(), q, kind)
            if not units:
                raise OrganizeError("清單上沒有要整理的")
            limit = max(0, int(max_videos or 0))
            todo = [u for u in units if not (kind != "held" and u.path in self._held or limit and _biggest(u) > limit)]
            if not todo:
                raise OrganizeError(f"清單上的 {len(units)} 個都標了「先不整理」" + (f"，或一次要送超過 {limit} 支影片" if limit else ""))
            if target == "path" and _dir_path(target_path) == "/":
                raise OrganizeError("請填要整理到哪個 115 資料夾")
            if not self.reorg.hold():
                raise OrganizeError("已經有一批在整理或刪除，等它完成再開始")
            self.batch = BatchJob(running=True, started=time.time(), total=len(todo), target=target or "auto",
                                  held=len(units) - len(todo), max_videos=limit)
        self.workers.start(self._organize_all, todo, target or "auto", target_path, cleanup)
        return self.batch.as_dict(results=False)

    def stop_all(self) -> dict:
        """做完手上這一個就停。"""
        if self.batch.running:
            self.batch.stopping = True
        return self.batch.as_dict(results=False)

    def _organize_all(self, units: List[Unit], target: str, target_path: str, cleanup: bool) -> None:
        job = self.batch
        errors = 0  # 連續出錯的預覽
        try:
            for u in units:
                if job.stopping or self._stop.is_set():
                    job.stopped = True
                    break
                job.current = u.name
                state = self._organize_one(u, target, target_path, cleanup, job)
                if state == RETRY:  # MoviePilot 連不上，這一個什麼都還沒做：等它回來再做一次
                    if not self._wait_mp(job):
                        break
                    job.current = u.name
                    state = self._organize_one(u, target, target_path, cleanup, job, retried=True)
                job.done += 1
                # 途中斷線、等太久：它可能還在背景做這一個，等它有回應、再等一下才送下一個，不要疊上去
                if state == DOWN and not self._wait_mp(job):
                    break
                errors = errors + 1 if state == ERROR else 0
                if errors >= BATCH_GIVE_UP:
                    job.error = f"連續 {errors} 個預覽出錯，先停下：{job.results[-1]['why']}"
                    break
            log.info("全部整理：%s 個資料夾，整理了 %s 個（%s 個檔案），跳過 %s 個，失敗 %s 個",
                     job.done, job.organized, job.files, job.skipped, job.failed)
            if job.files or job.queued or job.renamed:
                # MoviePilot 背景處理的也要同步：它做完之後本機的 strm 只靠增量同步讀 115 生活事件搬
                job.current = f"等 115 記下變動，{int(self.reorg.sync_delay)} 秒後同步"
                job.synced = self.reorg.sync_later()
        except Exception as exc:  # 背景執行緒：記下來，不讓網頁一直顯示「整理中」
            job.error = f"{type(exc).__name__}: {exc}"
            log.exception("全部整理時發生錯誤")
        finally:
            job.current = ""
            job.running = False
            job.finished = time.time()
            self._units_at = 0  # 清單重新算
            self.reorg.release()

    def _organize_one(self, u: Unit, target: str, target_path: str, cleanup: bool, job: BatchJob,
                      retried: bool = False) -> str:
        """預覽一個，沒問題就整理；回傳 OK、ERROR、RETRY、DOWN（見最上面）。retried：MoviePilot 連不上等過一次了，
        這次再連不上就記下來，不再重做。"""
        def record(kind: str, why: str) -> None:
            setattr(job, kind, getattr(job, kind) + 1)
            if len(job.results) < BATCH_RESULTS:
                job.results.append({"id": u.id, "name": u.name, "path": u.path, "kind": kind, "why": why})

        try:
            pv = self._preview(u, {}, target, target_path, scrape=False)
        except OrganizeError as exc:
            trouble = _mp_trouble(exc)
            if trouble == "offline" and not retried:
                return RETRY
            if trouble:
                record("skipped", f"MoviePilot 預覽時{DOWN_WORDS.get(trouble, '連不上')}：{exc}"
                       + ("。可能是資料夾太大，它可能還在背景預覽；先跳過" if trouble != "offline" else ""))
                return DOWN
            record("skipped", str(exc))
            return ERROR
        if pv["review"]:
            record("skipped", "；".join(pv["review"]))
            return OK
        if not pv["token"]:
            job.nothing += 1
            return OK
        plan = self.reorg.take_preview(pv["token"])
        run = ReorgJob()
        folders = [{"cid": int(c["cid"]), "path": c["path"]} for c in u.cleanup_folders()] if cleanup else []
        self.reorg.run_plan(plan, folders, run)
        if run.down == "offline" and not (run.done or run.queued or run.renamed) and not retried:
            return RETRY  # 第一批就連不上：什麼都沒送出去，等它回來重新預覽、整理
        job.files += run.done
        job.queued += run.queued
        job.renamed += run.renamed
        if run.done or run.queued or run.renamed:
            job.organized += 1
        if run.down:
            record("failed", f"MoviePilot 整理途中{DOWN_WORDS.get(run.down, '連不上')}：{run.errors[-1]}。"
                   "送出去的它可能還在背景整理，沒送的這次不送；同步後再看這個資料夾")
            return DOWN
        if run.failed or run.errors:
            bad = next((i for i in run.items if i["state"] not in ("completed", "accepted", "retry_wait", "kept", "removed")), None)
            record("failed", run.errors[0] if run.errors else f"{bad['name']}：{bad['message'] or bad['state']}" if bad else "有檔案整理失敗")
        return OK

    def _wait_mp(self, job: BatchJob) -> bool:
        """MoviePilot 連不上、途中斷線或等太久：等它有回應，再多等一下（它可能還在做剛才那個）才繼續。
        等太久（job.error 寫明）、按了停止、程式要結束回傳 False。"""
        job.mp_down += 1
        started = time.monotonic()
        while not self.mp.reachable():
            waited = time.monotonic() - started
            if waited >= MP_DOWN_WAIT:
                job.error = (f"MoviePilot 連不上超過 {MP_DOWN_WAIT // 60} 分鐘，先停下。它可能被系統停掉了（記憶體不夠？），"
                             "看它的日誌，重新啟動後再按「全部整理」")
                if ":3000" in (self.mp.cfg.url or ""):
                    job.error += FRONTEND_HINT
                return False
            job.current = f"MoviePilot 連不上，等它恢復（已等 {int(waited // 60)} 分鐘，最多等 {MP_DOWN_WAIT // 60} 分鐘）"
            if self._pause(job, MP_DOWN_POLL):
                return False
        job.current = f"MoviePilot 有回應了，等 {MP_COOLDOWN} 秒讓它做完手上的再繼續"
        return not self._pause(job, MP_COOLDOWN)

    def _pause(self, job: BatchJob, seconds: float) -> bool:
        """等幾秒，每秒看一次；按了停止或程式要結束回傳 True。"""
        end = time.monotonic() + seconds
        while True:
            if job.stopping or self._stop.is_set():
                job.stopped = True
                return True
            left = end - time.monotonic()
            if left <= 0:
                return False
            self._stop.wait(min(1.0, left))

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


SKIP_LABELS = {"same": "已經照格式命名", "outside": "會搬出同步目錄", "latest": "在同一個資料夾裡改名、覆蓋模式是「保留最新」",
               "exists": "目標已經有同名檔案（多半是重複的）"}
NAMED_NOTE = "新名字由 MoviePilot 的「Mi302 整理助手」照它的整理規則算（和它整理的名字一樣，不必跑它的整理預覽）"
NO_EPISODE = "未识别到文件集数"  # MoviePilot（和外掛）認不出集號時的說明；番外、合集時是「…，识别为特典/附加视频文件」
VAGUE_FAILURE = "整理任务处理失败"  # MoviePilot 預覽時對不上目錄設定、算不出計畫都只說這句，原因只寫在它的日誌


def _view(r: dict, part: Part, roots: List[str], overwrite: str = "never") -> dict:
    """MoviePilot 預覽的一個檔案，加上 Mi302 的檢查（MoviePilot 預覽時不看覆蓋模式，沒有擋來源等於目標，
    也不知道同步目錄在哪）。overwrite：這次目標媒體庫目錄的覆蓋模式。"""
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
    elif ok and overwrite == "latest" and posixpath.dirname(target) == posixpath.dirname(source.rstrip("/")):
        # 目標還不存在時，「保留最新」會先刪掉目標資料夾裡同一集的其他版本，只避開目標本身：來源檔案也在那裡，會被刪掉
        ok, skip, message = False, "latest", ("MoviePilot 這個媒體庫目錄的覆蓋模式是「保留最新」：在同一個資料夾裡改名時，"
                                              "它會先刪掉同一集的其他版本，連這個檔案本身也會刪掉。不送；要整理請先把覆蓋模式改成「不覆蓋」")
    elif ok and not inside(target):
        if inside(source):
            ok, skip, message = False, "outside", "新位置不在 Mi302 的 115 同步目錄裡，整理後會從媒體庫消失，不送"
        else:
            warnings.append("新位置不在 Mi302 的 115 同步目錄裡，Mi302 不會替它產生 strm")
    if not ok and not message:
        message = "MoviePilot 沒有說明原因"
    elif not ok and VAGUE_FAILURE in message:
        message += "（MoviePilot 預覽時不說原因，詳情在它的日誌；常見是目錄設定對不上這個位置，可以把「整理到」改成「同一層」或指定的資料夾）"
    return {"name": posixpath.basename(source.rstrip("/")), "source": source, "target": target, "episode": episode,
            "season": season, "title": str(r.get("title") or ""), "type": str(r.get("type") or ""), "ok": ok,
            "skip": skip, "message": message, "warnings": warnings, "part": part.label}


def _summary(items: List[dict], review: List[str]) -> dict:
    """預覽的統計；MoviePilot 不會整理的、要看一下的也寫進 review（要人看的原因）最前面。"""
    failed = [i for i in items if not i["ok"] and not i["skip"]]
    warned = [i for i in items if i["ok"] and i["warnings"]]
    if failed:
        review.insert(0, f"{len(failed)} 個 MoviePilot 不會整理（{failed[0]['message']}）")
    if warned:
        review.insert(0, f"{len(warned)} 個要看一下（{warned[0]['warnings'][0]}）")
    return {"total": len(items), "ok": sum(1 for i in items if i["ok"]), "failed": len(failed),
            "skipped": sum(1 for i in items if i["skip"]), "warnings": len(warned)}


def _recognized(views: List[dict]) -> List[dict]:
    """MoviePilot 把這一部分認成什麼：片名、類型、季，各幾個檔案。"""
    counts = Counter((v["title"], v["type"], v["season"]) for v in views if v["ok"])
    return [{"title": t, "type": ty, "season": s, "count": n} for (t, ty, s), n in counts.most_common()]


def _check_recognized(unit: Unit, part: Part, o: dict, views: List[dict], recognized: List[dict], notes: List[str],
                      review: List[str], identity: List[str]) -> None:
    """MoviePilot 認成的對不對（這一部分沒指定的才看）：認成別部片的記進 identity（見 _identity_problems），
    認成的季和媒體庫不一樣的記進 review；都寫進 notes。"""
    if not o["tmdbid"]:
        for problem in _identity_problems(unit, part, views):
            notes.append(problem)
            identity.append(problem)
    if part.lib_season is not None and o["season"] is None:
        for r in recognized:
            if r["season"] is not None and r["season"] != part.lib_season and r["type"] != "电影":
                notes.append(f"「{part.label}」MoviePilot 認成第 {r['season']} 季，媒體庫裡是第 {part.lib_season} 季；"
                             "不對的話在這一部分指定季，再預覽一次")
                review.append(f"「{part.label}」MoviePilot 認成第 {r['season']} 季，媒體庫裡是第 {part.lib_season} 季")


TMDB_IN_NAME = re.compile(r"tmdb(?:id)?\s*[=\-]\s*(\d+)", re.I)
YEAR_IN_NAME = re.compile(r"(?<!\d)(19\d\d|20\d\d)(?!\d)")
TITLE_YEAR = re.compile(r"^(.*?)\s*\((\d{4})\)$")  # MoviePilot 預覽回傳的片名是「片名 (年份)」


def _loose(text: str) -> str:
    """比片名用：轉簡體、不分大小寫、去掉空白和標點。"""
    return re.sub(r"[\W_]+", "", simplified(text).casefold())


def _identity_problems(unit: Unit, part: Part, views: List[dict]) -> List[str]:
    """MoviePilot 認成了別部片：多半是資料夾名裡的 TMDB 編號錯了（CMS 這類工具寫的），它照編號查到另一部劇或電影，
    整理下去整部劇就搬進別部片的資料夾。兩種情況：
    - 檔名有集號，卻被認成電影（那個編號不是劇集，它改查同一個編號的電影）；
    - 認出來的片名不在資料夾名、檔名、媒體庫的名稱裡，年份也差超過一年（例如 2018 年的劇被認成 2023 年的續集）。
    只看片名或只看年份都不算：英文資料夾認成中文片名、首播年份和資料夾差一年都很常見。"""
    ok = [v for v in views if v["ok"] and v["title"]]
    ids = TMDB_IN_NAME.findall(part.remote)
    hint = f"資料夾名裡的 TMDB 編號 {ids[-1]} 多半不對，" if ids else ""
    problems = []
    movies = Counter(v["title"] for v in ok if v["type"] == "电影" and v["episode"] is not None)
    for title, n in movies.most_common(1):
        problems.append(f"「{part.label}」有 {n} 個有集號的檔案被 MoviePilot 認成電影「{title}」：{hint}"
                        "請在這一部分指定類型和正確的 TMDB 編號")
    found = YEAR_IN_NAME.search(unit.name)
    year = unit.year or (int(found.group(1)) if found else None)
    if not year:
        return problems
    names = [_loose(n) for n in (unit.name, unit.title, part.label, posixpath.basename(part.remote.rstrip("/")))]
    for title in sorted({v["title"] for v in ok} - set(movies)):
        m = TITLE_YEAR.match(title)
        short = _loose(m.group(1)) if m else ""
        if not short or abs(int(m.group(2)) - year) <= 1:
            continue
        if any(short in n for n in names) or any(short in _loose(v["name"]) for v in ok if v["title"] == title):
            continue
        problems.append(f"「{part.label}」MoviePilot 認成「{title}」，片名和年份（{year}）都和資料夾對不上：{hint}"
                        "請確認，或在這一部分指定正確的 TMDB 編號")
    return problems


def _mark_duplicates(views: List[dict]) -> None:
    """兩個檔案整理到同一個位置：後整理的會被 MoviePilot 跳過，提醒一下。"""
    seen = Counter(v["target"] for v in views if v["ok"] and v["target"])
    for v in views:
        if v["ok"] and seen.get(v["target"], 0) > 1:
            v["warnings"].append(f"有 {seen[v['target']]} 個檔案會整理到同一個位置，只會留一個")
