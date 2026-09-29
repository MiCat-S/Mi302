"""整理 115 網盤：找出命名不照 MoviePilot 重命名格式的資料夾，整個交給 MoviePilot 整理。

MoviePilot 整理時照它的重命名格式（TV_RENAME_FORMAT、MOVIE_RENAME_FORMAT，Jinja2 模板）組出新路徑，例如
「凡人修仙传 (2020) {tmdbid=106449}/Season 1/凡人修仙传 - S01E176 - 第 176 集.mp4」。這裡把模板變成正則，
拿媒體庫裡每一部劇、每一部電影在 115 上的資料夾去比，三層都看：
- 資料夾名稱：多了「更176｜停更」這種字、{tmdbid-106449} 這種寫法、少了 {tmdbid=…}；有刮削資料（nfo）時，
  也和照格式組出來的名稱比。
- 季資料夾：劇集的影片直接放在劇集資料夾裡，或季資料夾不叫「Season 1」這種名稱（模板有季資料夾時）。
- 檔名：176.mp4、第10集.mp4 這種。
對不上的一個資料夾一列，附上原因。只用媒體庫和 115 同步紀錄（p115_index），不向 115 請求。
電影和其他電影混放在同一個資料夾的，一支一列（沒有自己的資料夾）。

整理時照 MoviePilot 網頁「檔案管理 → 整理」的做法，把整個資料夾（有季資料夾的就一季一個）當成一個項目交給
MoviePilot，影片、字幕、音軌一起搬到同一層、照它的格式命名；已經有同名的標準資料夾（例如「康熙来了 (2004)」
旁邊的「康熙来了 (2004) {tmdbid=6836}」）就併進去。季一定明確給：資料夾名稱裡的「预计第二季度」
MoviePilot 會認成第 2 季。MoviePilot 的預覽不看目標有沒有檔案，也沒有「來源等於目標」的保護，所以 Mi302
自己擋：新位置和原本一樣、或會搬出同步目錄的，整個項目都不送。
執行、清掉搬空的舊資料夾、之後的增量同步沿用 reorganize.Reorganizer。
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import re
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .db import Database
from .filetypes import VIDEO_EXTS
from .moviepilot import MoviePilot, MoviePilotError
from .p115 import P115Error
from .scanner import parse_season_dir
from .strm_sync import remote_root, task_key

log = logging.getLogger(__name__)

# MoviePilot 的預設重命名格式（app/runtime/config.py）；讀不到使用者的設定時用它比
DEFAULT_TV = ("{{title}}{% if year %} ({{year}}){% endif %}/Season {{season}}/{{title}} - {{season_episode}}"
              "{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif %}{{fileExt}}")
DEFAULT_MOVIE = ("{{title}}{% if year %} ({{year}}){% endif %}/{{title}}{% if year %} ({{year}}){% endif %}"
                 "{% if part %}-{{part}}{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}{{fileExt}}")
SPECIAL_SEASON_DIRS = {"specials", "sps"}  # MoviePilot 的 RENAME_FORMAT_S0_NAMES：第 0 季也可以叫這個

# 資料夾名稱裡的 {tmdbid=6836}、[tmdb=6836]、{tmdbid-106449}（MoviePilot 都認得）
TMDB_TAG_RE = re.compile(r"\s*[\[{（(]\s*tmdb(?:id)?\s*[=:\-_]\s*(\d+)\s*[\]}）)]", re.I)
SCAN_TTL = 300  # 找出來的清單幾秒內直接用
FORMATS_TTL = 600
PREVIEW_TIMEOUT = 600

# 模板變數對應的正則；沒列的（片名、集名…）是任意文字
VAR_PATTERNS = {
    "year": r"\d{4}", "season": r"\d{1,4}", "season_fmt": r"S\d{1,4}", "episode": r"\d{1,4}",
    "season_episode": r"S\d{1,4}E\d{1,4}(?:-?E\d{1,4})?", "tmdbid": r"\d+", "imdbid": r"tt\d+", "doubanid": r"\d+",
    "fileExt": r"\.[A-Za-z0-9]+",
}
TOKEN_RE = re.compile(r"(\{\{.*?\}\}|\{%-?.*?-?%\}|\{#.*?#\})", re.S)
NAME_RE = re.compile(r"^[A-Za-z_]\w*$")


class OrganizeError(Exception):
    pass


class TemplateError(Exception):
    pass


def folder_tag(name: str) -> str:
    """資料夾名稱裡的 TMDB 編號，沒有是空字串。"""
    m = TMDB_TAG_RE.search(name)
    return m.group(1) if m else ""


def folder_key(name: str) -> str:
    """比對同一部用的名稱：去掉 tmdbid 標記，全形括號、數字轉半形，空白合併，不分大小寫。"""
    text = unicodedata.normalize("NFKC", TMDB_TAG_RE.sub(" ", name))
    text = re.sub(r"\s*\(\s*", " (", text)
    text = re.sub(r"\s*\)", ")", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


# ---------------- 命名格式（Jinja2 模板的一小部分）→ 正則 ----------------


@dataclass
class _Var:
    name: str  # 看不懂的運算式是空字串


@dataclass
class _If:
    branches: List[Tuple[Optional[str], list]]  # (條件, 內容)；else 的條件是 None


def _var_name(expr: str) -> str:
    """{{ title }}、{{title | upper}} → title；{{a or b}}、{{x.y}} 這種看不懂的回傳空字串。"""
    name = expr.split("|", 1)[0].strip()
    return name if NAME_RE.match(name) else ""


def _parse(template: str) -> list:
    """把模板拆成文字、變數、if 區塊；for、set 這類不支援就丟 TemplateError。"""
    root: list = []
    stack: List[Tuple[list, Optional[_If]]] = [(root, None)]
    for token in TOKEN_RE.split(template):
        if not token or token.startswith("{#"):
            continue
        out, block = stack[-1]
        if token.startswith("{{"):
            out.append(_Var(_var_name(token[2:-2])))
        elif token.startswith("{%"):
            stmt = token.strip("{%-} \t\n")
            word, _, rest = stmt.partition(" ")
            if word == "if":
                node = _If([(rest.strip(), [])])
                out.append(node)
                stack.append((node.branches[0][1], node))
            elif word in ("elif", "else") and block is not None:
                block.branches.append((rest.strip() if word == "elif" else None, []))
                stack[-1] = (block.branches[-1][1], block)
            elif word == "endif" and block is not None:
                stack.pop()
            else:
                raise TemplateError(f"不支援模板裡的「{token}」")
        else:
            out.append(token)
    if len(stack) != 1:
        raise TemplateError("模板的 if 沒有 endif")
    return root


def _segments(nodes: list) -> List[list]:
    """照「/」切成每一層路徑；「/」出現在 if 裡面就不知道有幾層，丟 TemplateError。"""
    segs: List[list] = [[]]
    for node in nodes:
        if isinstance(node, str):
            parts = node.split("/")
            segs[-1].append(parts[0])
            for part in parts[1:]:
                segs.append([part] if part else [])
        else:
            if isinstance(node, _If) and any("/" in _plain(b) for _, b in node.branches):
                raise TemplateError("模板在 if 裡面換資料夾，看不出有幾層")
            segs[-1].append(node)
    return segs


def _plain(nodes: list) -> str:
    return "".join(n if isinstance(n, str) else _plain(sum((b for _, b in n.branches), [])) if isinstance(n, _If) else ""
                   for n in nodes)


def _regex(nodes: list, drop_ext: bool = False) -> str:
    out = []
    for node in nodes:
        if isinstance(node, str):
            out.append(re.escape(node))
        elif isinstance(node, _Var):
            out.append("" if drop_ext and node.name == "fileExt" else VAR_PATTERNS.get(node.name, r"[^/]+?"))
        else:
            alts = [_regex(body, drop_ext) for _, body in node.branches]
            if node.branches[-1][0] is not None:  # 沒有 else：整段可以不出現
                alts.append("")
            out.append("(?:" + "|".join(alts) + ")")
    return "".join(out)


def _render(nodes: list, values: dict) -> Optional[str]:
    """用知道的值組出名稱；用到不知道的變數或看不懂的條件時回傳 None。"""
    out = []
    for node in nodes:
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, _Var):
            if node.name not in values:
                return None
            out.append(str(values[node.name] or ""))
        else:
            for cond, body in node.branches:
                if cond is not None and (not NAME_RE.match(cond) or cond not in values):
                    return None
                if cond is None or values[cond]:
                    text = _render(body, values)
                    if text is None:
                        return None
                    out.append(text)
                    break
    return "".join(out)


@dataclass
class _Layout:
    """一種媒體的命名格式：資料夾、季資料夾、檔名三層各自的正則（檔名不含副檔名，本機只有 strm 的名稱）。"""

    folder: Optional[list] = None
    season: Optional[list] = None
    file: list = field(default_factory=list)
    _compiled: Dict[str, "re.Pattern"] = field(default_factory=dict, repr=False)

    def match(self, part: str, text: str) -> bool:
        nodes = getattr(self, part)
        if nodes is None:
            return True
        if part not in self._compiled:
            self._compiled[part] = re.compile(_regex(nodes, drop_ext=part == "file"), re.S)
        return self._compiled[part].fullmatch(text) is not None

    def render_folder(self, values: dict) -> Optional[str]:
        return _render(self.folder, values) if self.folder is not None else None

    def uses(self, part: str, var: str) -> bool:
        nodes = getattr(self, part) or []
        return re.search(r"\{\{\s*" + var + r"\b", _source(nodes)) is not None


def _source(nodes: list) -> str:
    out = []
    for n in nodes:
        if isinstance(n, str):
            out.append(n)
        elif isinstance(n, _Var):
            out.append("{{" + n.name + "}}")
        else:
            out.append(_source(sum((b for _, b in n.branches), [])))
    return "".join(out)


def _layout(template: str, tv: bool) -> _Layout:
    segs = _segments(_parse(template))
    if not segs[-1]:
        raise TemplateError("模板最後是「/」")
    if tv:
        return _Layout(folder=segs[-3] if len(segs) >= 3 else segs[-2] if len(segs) == 2 else None,
                       season=segs[-2] if len(segs) >= 3 else None, file=segs[-1])
    return _Layout(folder=segs[-2] if len(segs) >= 2 else None, file=segs[-1])


@dataclass
class Formats:
    tv_template: str
    movie_template: str
    source: str  # moviepilot / default
    note: str
    tv: _Layout
    movie: _Layout

    @classmethod
    def build(cls, tv: str, movie: str, source: str, note: str = "") -> "Formats":
        notes = [note] if note else []
        try:
            tv_layout = _layout(tv, True)
        except TemplateError as exc:
            notes.append(f"劇集的命名格式看不懂（{exc}），改用 MoviePilot 的預設格式比對")
            tv, tv_layout = DEFAULT_TV, _layout(DEFAULT_TV, True)
        try:
            movie_layout = _layout(movie, False)
        except TemplateError as exc:
            notes.append(f"電影的命名格式看不懂（{exc}），改用 MoviePilot 的預設格式比對")
            movie, movie_layout = DEFAULT_MOVIE, _layout(DEFAULT_MOVIE, False)
        return cls(tv, movie, source, "；".join(notes), tv_layout, movie_layout)

    def view(self) -> dict:
        return {"tv": self.tv_template, "movie": self.movie_template, "source": self.source, "note": self.note}


# ---------------- 找出不規範的資料夾 ----------------


@dataclass
class Part:
    """一部劇要分幾次送：整個資料夾、每個季資料夾、直接放在劇集資料夾裡的影片。"""

    key: str
    label: str
    cid: int  # 送整個資料夾時是它的 id；loose 時是放影片的那個資料夾
    rel: str  # 相對同步任務本機資料夾的路徑
    videos: int
    season: Optional[int] = None
    loose: bool = False  # 只送直接放在這個資料夾裡的影片（旁邊還有季資料夾）
    stems: List[str] = field(default_factory=list)  # loose 時要送的影片（本機 strm 的檔名，不含副檔名）


@dataclass
class Unit:
    id: str  # d{資料夾 id}；沒有自己資料夾的電影是 f{檔案 id}
    kind: str  # series / movie / movie_file
    task: object
    rel: str
    path: str  # 115 上的完整路徑
    cid: int  # 資料夾 id；movie_file 是它所在的資料夾
    parent_cid: int
    name: str
    item_id: int
    title: str
    year: Optional[int]
    reasons: List[str]
    videos: int
    tmdbid: str = ""
    tmdb_from: str = ""  # name / library / merge
    expected: str = ""  # 有刮削資料時照格式該叫什麼
    merge_into: Optional[dict] = None
    parts: List[Part] = field(default_factory=list)

    @property
    def parent(self) -> str:
        return posixpath.dirname(self.path)

    def view(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "name": self.name, "path": self.path, "parent": self.parent,
            "item_id": self.item_id, "title": self.title, "year": self.year, "reasons": self.reasons,
            "videos": self.videos, "tmdbid": self.tmdbid, "tmdb_from": self.tmdb_from, "expected": self.expected,
            "merge_into": self.merge_into, "type": "movie" if self.kind != "series" else "tv",
            "parts": [{"key": p.key, "label": p.label, "videos": p.videos, "season": p.season} for p in self.parts],
        }


def _tmdb_of(provider_ids: Optional[str]) -> str:
    try:
        return str((json.loads(provider_ids or "{}") or {}).get("Tmdb") or "")
    except ValueError:
        return ""


class _Tree:
    """一個同步任務在 115 同步紀錄裡的目錄樹（本機的相對路徑）。"""

    def __init__(self, db: Database, task):
        key = task_key(task)
        self.dirs: Dict[str, int] = {r["path"]: r["file_id"] for r in
                                     db.query("SELECT file_id, path FROM p115_index WHERE task=? AND is_dir=1", (key,))}
        self.children: Dict[str, List[str]] = {}
        for d in self.dirs:
            self.children.setdefault(posixpath.dirname(d), []).append(posixpath.basename(d))
        self.strm: Dict[str, List[Tuple[int, str]]] = {}  # 資料夾 → [(檔案 id, 影片檔名不含副檔名)]
        for r in db.query("SELECT file_id, path FROM p115_index WHERE task=? AND is_dir=0", (key,)):
            stem, ext = posixpath.splitext(r["path"])
            if ext.lower() == ".strm" or ext.lower() in VIDEO_EXTS:
                self.strm.setdefault(posixpath.dirname(r["path"]), []).append((r["file_id"], posixpath.basename(stem)))
        self.total: Dict[str, int] = Counter()  # 資料夾（含子資料夾）裡有幾支影片
        for folder, videos in self.strm.items():
            d = folder
            while True:
                self.total[d] += len(videos)
                if not d:
                    break
                d = posixpath.dirname(d)

    def walk(self, rel: str) -> List[str]:
        """這個資料夾和底下所有子資料夾。"""
        out, stack = [], [rel]
        while stack:
            d = stack.pop()
            out.append(d)
            stack += [posixpath.join(d, c) if d else c for c in self.children.get(d, [])]
        return out

    def stems_under(self, rel: str) -> List[str]:
        return [s for d in self.walk(rel) for _, s in self.strm.get(d, [])]


def _season_mode(seasons: Iterable[Optional[int]]) -> Optional[int]:
    counts = Counter(s for s in seasons if s is not None and s >= 0)
    return counts.most_common(1)[0][0] if counts else None


def _examples(names: List[str]) -> str:
    shown = "、".join(f"「{n}」" for n in names[:2])
    return shown + (f" 等 {len(names)} 個" if len(names) > 2 else "")


def find_units(db: Database, tasks, formats: Formats) -> List[Unit]:
    """媒體庫裡在同步目錄底下的劇集、電影，命名不照格式的都列出來（照 115 路徑排）。"""
    units: List[Unit] = []
    for task in tasks:
        local = str(Path(task.local).expanduser())
        tree = _Tree(db, task)
        lo, hi = local + "/", local + "/\U0010ffff"
        episodes: Dict[str, List[Optional[int]]] = {}  # 本機資料夾 → 裡面每一集的季
        for r in db.query("SELECT path, parent_index_number FROM items WHERE type='Episode' AND is_strm=1 AND path>=? AND path<?",
                          (lo, hi)):
            episodes.setdefault(posixpath.dirname(r["path"]), []).append(r["parent_index_number"])
        for s in db.query("SELECT id, name, year, path, provider_ids FROM items WHERE type='Series' AND path>=? AND path<?",
                          (lo, hi)):
            unit = _check_series(task, tree, s, local, episodes, formats)
            if unit:
                units.append(unit)
        for m in db.query("SELECT id, name, year, path, provider_ids FROM items WHERE type='Movie' AND is_strm=1 "
                          "AND path>=? AND path<?", (lo, hi)):
            unit = _check_movie(task, tree, m, local, formats)
            if unit:
                units.append(unit)
    for u in units:
        if not u.tmdbid and u.merge_into and u.merge_into.get("tmdbid"):
            u.tmdbid, u.tmdb_from = u.merge_into["tmdbid"], "merge"
    units.sort(key=lambda u: u.path)
    return units


def _merge_target(tree: _Tree, rel: str, name: str, tmdbid: str, layout: _Layout, root: str) -> Optional[dict]:
    """同一層已經照格式命名、看起來是同一部的資料夾（同名去掉標記後一樣，或 tmdbid 一樣）。"""
    parent = posixpath.dirname(rel)
    key = folder_key(name)
    for sib in tree.children.get(parent, []):
        if sib == name or not layout.match("folder", sib):
            continue
        tag = folder_tag(sib)
        if folder_key(sib) == key or (tmdbid and tag == tmdbid):
            sib_rel = posixpath.join(parent, sib) if parent else sib
            return {"name": sib, "path": posixpath.join(root, sib_rel), "cid": tree.dirs.get(sib_rel), "tmdbid": tag}
    return None


def _name_reasons(name: str, layout: _Layout, expected: Optional[str]) -> List[str]:
    reasons = []
    tag = folder_tag(name)
    if not layout.match("folder", name):
        if tag and layout.uses("folder", "tmdbid") and not re.search(r"\{tmdbid=\d+\}", name):
            reasons.append("資料夾名稱不照命名格式（tmdbid 的寫法不對）")
        else:
            reasons.append("資料夾名稱不照命名格式")
    elif tag and not layout.uses("folder", "tmdbid"):
        reasons.append("資料夾名稱帶 tmdbid 標記，MoviePilot 的命名格式不會有")
    if expected and expected != name and not reasons:
        reasons.append("資料夾名稱和刮削資料對不上")
    return reasons


def _check_series(task, tree: _Tree, s, local: str, episodes: Dict[str, List[Optional[int]]], formats: Formats) -> Optional[Unit]:
    rel = Path(s["path"]).relative_to(local).as_posix() if s["path"].startswith(local + "/") else ""
    cid = tree.dirs.get(rel)
    if not rel or not cid or not tree.total.get(rel):
        return None
    layout, root = formats.tv, remote_root(task)
    name = posixpath.basename(rel)
    tmdb_lib = _tmdb_of(s["provider_ids"])
    expected = layout.render_folder({"title": s["name"], "year": s["year"] or "", "tmdbid": tmdb_lib}) if tmdb_lib else None
    reasons = _name_reasons(name, layout, expected)
    loose = tree.strm.get(rel, [])
    subdirs = [c for c in tree.children.get(rel, []) if tree.total.get(posixpath.join(rel, c))]
    if layout.season is not None:
        if loose:
            reasons.append(f"{len(loose)} 支影片直接放在劇集資料夾裡，沒有季資料夾")
        bad = [c for c in subdirs if c.lower() not in SPECIAL_SEASON_DIRS and not layout.match("season", c)]
        if bad:
            reasons.append("季資料夾名稱不照命名格式：" + _examples(bad))
    bad_files = [stem for stem in tree.stems_under(rel) if not layout.match("file", stem)]
    if bad_files:
        reasons.append(f"{len(bad_files)} 個檔名不照命名格式，例如「{bad_files[0]}」")
    if not reasons:
        return None

    def season_of(folder_rel: str, only_here: bool) -> Optional[int]:
        """媒體庫裡這個資料夾（only_here=False 時含子資料夾）的集大多是第幾季。"""
        folders = [folder_rel] if only_here else tree.walk(folder_rel)
        return _season_mode(v for f in folders for v in episodes.get(str(Path(local) / f), []))

    parts: List[Part] = []
    if not subdirs:
        parts.append(Part("all", "整個資料夾", cid, rel, len(loose), season_of(rel, True) or 1))
    else:
        for c in sorted(subdirs):
            c_rel = posixpath.join(rel, c)
            season = parse_season_dir(c)
            parts.append(Part(f"d{tree.dirs[c_rel]}", c, tree.dirs[c_rel], c_rel, tree.total[c_rel],
                              season if season is not None else season_of(c_rel, False)))
        if loose:
            parts.append(Part("loose", "直接放在劇集資料夾裡的影片", cid, rel, len(loose), season_of(rel, True) or 1,
                              loose=True, stems=[stem for _, stem in loose]))
    tag = folder_tag(name)
    unit = Unit(f"d{cid}", "series", task, rel, posixpath.join(root, rel), cid, tree.dirs.get(posixpath.dirname(rel), 0),
                name, s["id"], s["name"], s["year"], reasons, tree.total[rel],
                tmdbid=tag or tmdb_lib, tmdb_from="name" if tag else "library" if tmdb_lib else "",
                expected=expected if expected and expected != name else "", parts=parts)
    unit.merge_into = _merge_target(tree, rel, name, unit.tmdbid, layout, root)
    return unit


def _check_movie(task, tree: _Tree, m, local: str, formats: Formats) -> Optional[Unit]:
    if not m["path"].startswith(local + "/"):
        return None
    strm_rel = Path(m["path"]).relative_to(local).as_posix()
    folder, stem = posixpath.dirname(strm_rel), posixpath.splitext(posixpath.basename(strm_rel))[0]
    here = tree.strm.get(folder, [])
    fid = next((f for f, s in here if s == stem), None)
    if fid is None:
        return None
    layout, root = formats.movie, remote_root(task)
    tmdb_lib = _tmdb_of(m["provider_ids"])
    values = {"title": m["name"], "year": m["year"] or "", "tmdbid": tmdb_lib}
    own = bool(folder) and len(here) == 1 and folder in tree.dirs and not any(
        tree.total.get(posixpath.join(folder, c)) for c in tree.children.get(folder, []))
    if not own:
        if layout.folder is None:  # 格式本來就不分資料夾
            return None
        unit = Unit(f"f{fid}", "movie_file", task, strm_rel, posixpath.join(root, folder, stem), tree.dirs.get(folder, 0),
                    tree.dirs.get(posixpath.dirname(folder), 0) if folder else 0, stem, m["id"], m["name"], m["year"],
                    [f"沒有自己的資料夾（和另外 {len(here) - 1} 支影片放在一起）" if len(here) > 1 else "沒有自己的資料夾"], 1,
                    tmdbid=tmdb_lib, tmdb_from="library" if tmdb_lib else "",
                    parts=[Part("file", "這支影片", tree.dirs.get(folder, 0), folder, 1, None, loose=True, stems=[stem])])
        return unit
    name = posixpath.basename(folder)
    expected = layout.render_folder(values) if tmdb_lib else None
    reasons = _name_reasons(name, layout, expected)
    if not layout.match("file", stem):
        reasons.append(f"檔名不照命名格式：「{stem}」")
    if not reasons:
        return None
    cid = tree.dirs[folder]
    tag = folder_tag(name)
    unit = Unit(f"d{cid}", "movie", task, folder, posixpath.join(root, folder), cid, tree.dirs.get(posixpath.dirname(folder), 0),
                name, m["id"], m["name"], m["year"], reasons, tree.total.get(folder, 1),
                tmdbid=tag or tmdb_lib, tmdb_from="name" if tag else "library" if tmdb_lib else "",
                expected=expected if expected and expected != name else "",
                parts=[Part("all", "整個資料夾", cid, folder, tree.total.get(folder, 1))])
    unit.merge_into = _merge_target(tree, folder, name, unit.tmdbid, layout, root)
    return unit


# ---------------- 清單、預覽 ----------------


class Organizer:
    def __init__(self, db: Database, strm_sync, moviepilot: MoviePilot, reorganizer):
        self.db = db
        self.strm_sync = strm_sync
        self.mp = moviepilot
        self.reorg = reorganizer
        self._lock = threading.Lock()
        self._units: Dict[str, Unit] = {}
        self._order: List[str] = []
        self._scanned = 0.0
        self._formats: Optional[Formats] = None
        self._formats_at = 0.0

    @property
    def p115(self):
        return self.strm_sync.p115

    def formats(self) -> Formats:
        """MoviePilot 的重命名格式（十分鐘內用上次讀到的）；讀不到就用它的預設格式。"""
        if self._formats and time.time() - self._formats_at < FORMATS_TTL:
            return self._formats
        note = ""
        tv = movie = ""
        if self.mp.can_subscribe:
            try:
                tv, movie = self.mp.rename_formats()
            except MoviePilotError as exc:
                note = f"讀不到 MoviePilot 的命名格式（{exc}），先用它的預設格式比對"
        else:
            note = "MoviePilot 沒有填帳號密碼，讀不到它的命名格式，先用預設格式比對"
        self._formats = Formats.build(tv or DEFAULT_TV, movie or DEFAULT_MOVIE, "moviepilot" if tv else "default", note)
        self._formats_at = time.time()
        return self._formats

    def scan(self, refresh: bool = False) -> List[Unit]:
        with self._lock:
            if refresh or not self._units and not self._scanned or time.time() - self._scanned > SCAN_TTL:
                if refresh:
                    self._formats_at = 0
                units = find_units(self.db, self.strm_sync.tasks, self.formats())
                self._units = {u.id: u for u in units}
                self._order = [u.id for u in units]
                self._scanned = time.time()
            return [self._units[i] for i in self._order]

    def list(self, q: str = "", kind: str = "", offset: int = 0, limit: int = 50, refresh: bool = False) -> dict:
        units = self.scan(refresh)
        counts = Counter(u.kind for u in units)
        text = q.strip().casefold()
        shown = [u for u in units if (not kind or u.kind == kind or (kind == "movie" and u.kind == "movie_file"))
                 and (not text or text in u.name.casefold() or text in (u.title or "").casefold() or text in u.path.casefold())]
        return {
            "formats": self.formats().view(), "scanned": self._scanned, "total": len(shown),
            "counts": {"series": counts["series"], "movie": counts["movie"] + counts["movie_file"]},
            "items": [u.view() for u in shown[offset:offset + limit]],
        }

    def nonstandard_series(self) -> Set[int]:
        """媒體庫裡資料夾不規範的劇（給「集號不對的劇」標出來）。"""
        return {u.item_id for u in self.scan() if u.kind == "series"}

    # ---------------- 預覽 ----------------

    def preview(self, unit_id: str, tmdbid: str, mtype: str, seasons: Dict[str, Optional[int]], scrape: bool = True) -> dict:
        """請 MoviePilot 只算不做，一個部分（整個資料夾、一季、直接放著的影片）一次；有能送的就給預覽代碼。"""
        self.scan()
        unit = self._units.get(unit_id)
        if not unit:
            raise OrganizeError("清單已經更新，找不到這個資料夾，請重新找一次")
        tmdbid = str(tmdbid or "").strip()
        if tmdbid and not tmdbid.isdigit():
            raise OrganizeError("TMDB 編號要是數字")
        if mtype not in ("tv", "movie"):
            raise OrganizeError("請選類型（電視劇或電影）")
        type_name = "电视剧" if mtype == "tv" else "电影"
        try:
            self.mp.check_transfer_preview()  # 舊版 MoviePilot 會把預覽當成真的整理
        except MoviePilotError as exc:
            raise OrganizeError(str(exc))
        roots = [remote_root(t) for t in self.strm_sync.tasks]
        expect = unit.merge_into["path"] if unit.merge_into else ""
        items: List[dict] = []
        batches: List[dict] = []
        notes: List[str] = []
        listings: Dict[int, List[dict]] = {}
        for part in unit.parts:
            season = seasons.get(part.key, part.season) if mtype == "tv" else None
            if mtype == "tv" and season is None:
                notes.append(f"「{part.label}」沒有填第幾季，這次不送")
                continue
            try:
                fileitems, single = self._fileitems(unit, part, listings)
            except P115Error as exc:
                raise OrganizeError(f"讀不到 115 上的檔案：{exc}")
            if not fileitems:
                notes.append(f"「{part.label}」在 115 上找不到影片，這次不送（先同步一次）")
                continue
            try:
                results = self.mp.transfer(fileitems, tmdbid or None, season, None, scrape, unit.parent, preview=True,
                                           mtype=type_name, timeout=PREVIEW_TIMEOUT, single=single)
            except MoviePilotError as exc:
                raise OrganizeError(f"MoviePilot 預覽失敗：{exc}")
            views = [self._view(r, part, roots, expect, mtype) for r in results]
            _mark_duplicates(views)
            blocked = [v for v in views if v["blocked"]]
            if blocked:
                notes.append(f"「{part.label}」有 {len(blocked)} 個檔案不能整理（{blocked[0]['message']}），"
                             "MoviePilot 是整個資料夾一起整理，所以這一部分都不送")
                for v in views:
                    v["ok"] = False
            items += views
            ok = sum(1 for v in views if v["ok"])
            if ok:
                batches.append({"fileitems": fileitems, "single": single, "season": season, "count": len(views),
                                "label": part.label, "local": str(Path(unit.task.local).expanduser() / part.rel)})
        folders = sorted({_top_folder(v["target"], unit.parent) for v in items if v["ok"] and v["target"]} - {""})
        if len(folders) > 1:
            notes.append("會整理到 " + str(len(folders)) + " 個資料夾：" + "、".join(f"「{f}」" for f in folders[:3]))
        token = self._remember(unit, batches, tmdbid, type_name, scrape) if batches else None
        return {
            "token": token, "items": items, "notes": notes, "folders": folders,
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

    @staticmethod
    def _view(r: dict, part: Part, roots: List[str], expect: str, mtype: str) -> dict:
        source = str(r.get("source") or "")
        target = str(r.get("target") or r.get("target_dir") or "")
        episode = int(r["episode"]) if str(r.get("episode") or "").isdigit() else None
        ok = bool(r.get("success")) and bool(target)
        message, warnings, blocked = str(r.get("message") or ""), [], False
        if ok and not any(target.startswith(root.rstrip("/") + "/") for root in roots):
            ok, blocked, message = False, True, "新位置不在 Mi302 的 115 同步目錄裡，整理後會從媒體庫消失"
        elif ok and target.rstrip("/") == source.rstrip("/"):
            ok, blocked, message = False, True, "新位置和原本一樣（已經照格式命名）"
        if ok and expect and not target.startswith(expect.rstrip("/") + "/"):
            warnings.append(f"不會併進「{posixpath.basename(expect)}」：MoviePilot 的命名格式組出來的資料夾名稱不一樣")
        if ok and mtype == "tv" and episode is None and posixpath.splitext(source)[1].lower() in VIDEO_EXTS:
            warnings.append("MoviePilot 沒有說是第幾集")
        if not ok and not message:
            message = "MoviePilot 沒有說明原因"
        return {"name": posixpath.basename(source.rstrip("/")), "source": source, "target": target, "episode": episode,
                "season": r.get("season"), "ok": ok, "blocked": blocked, "message": message, "warnings": warnings,
                "part": part.label}

    def _remember(self, unit: Unit, batches: List[dict], tmdbid: str, type_name: str, scrape: bool) -> str:
        payload = {"plan_id": f"o{unit.id}", "mode": "organize", "title": unit.path, "cid": unit.cid if unit.kind != "movie_file" else None,
                   "tmdbid": tmdbid, "type_name": type_name, "season": None, "target_path": unit.parent, "scrape": bool(scrape),
                   "batches": batches, "files": {}}
        token = hashlib.sha1(json.dumps({k: payload[k] for k in ("plan_id", "tmdbid", "type_name", "scrape", "batches")},
                                        sort_keys=True, default=str).encode()).hexdigest()[:16]
        self.reorg.remember_preview(token, payload)
        return token


def _mark_duplicates(views: List[dict]) -> None:
    """兩個檔案整理到同一個位置：後整理的會被 MoviePilot 跳過，提醒一下。"""
    seen = Counter(v["target"] for v in views if v["ok"] and v["target"])
    for v in views:
        if v["ok"] and seen.get(v["target"], 0) > 1:
            v["warnings"].append(f"有 {seen[v['target']]} 個檔案會整理到同一個位置，只會留一個")


def _top_folder(target: str, parent: str) -> str:
    rest = target[len(parent.rstrip("/")) + 1:] if target.startswith(parent.rstrip("/") + "/") else ""
    return rest.split("/", 1)[0] if "/" in rest else ""
