"""媒體庫掃描：把磁碟上的 strm / 影片檔、NFO、圖片寫進 items 表。"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote

from .config import Config, LibraryConfig
from .db import Database
from .mediainfo import MediaInfoStore

log = logging.getLogger(__name__)

VIDEO_EXTS = {
    ".strm", ".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv",
    ".flv", ".webm", ".rmvb", ".mpg", ".mpeg", ".iso", ".3gp",
}
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

YEAR_RE = re.compile(r"(?:^|[\s.\-_\[(（])((?:19|20)\d{2})(?:$|[\s.\-_\])）])")
PAREN_YEAR_RE = re.compile(r"^(?P<title>.+?)\s*[(（\[](?P<year>(?:19|20)\d{2})[)）\]]")
EPISODE_PATTERNS = [
    re.compile(r"[Ss](?P<season>\d{1,3})[\s._-]*[Ee][Pp]?(?P<episode>\d{1,4})"),
    re.compile(r"(?P<season>\d{1,2})x(?P<episode>\d{1,3})(?!\d)"),
    re.compile(r"第\s*(?P<episode>\d{1,4}|[一二三四五六七八九十百零〇两兩]{1,6})\s*[集话話]"),
    re.compile(r"(?:^|[\s._\-\[])[Ee][Pp]?(?P<episode>\d{1,4})(?!\d)"),
]
SEASON_DIR_PATTERNS = [
    re.compile(r"^(?:season|series)[\s._-]*(?P<season>\d{1,3})$", re.I),
    re.compile(r"^s(?P<season>\d{1,3})$", re.I),
    re.compile(r"^第\s*(?P<season>\d{1,3}|[一二三四五六七八九十百零〇两兩]{1,4})\s*[季部]$"),
]
SPECIALS_DIRS = {"specials", "special", "sp", "特别篇", "特別篇"}
CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
# 分類資料夾（例如 電視劇/國產劇/劇名）最多往下找幾層
MAX_CATEGORY_DEPTH = 3
# 一次部分掃描超過這麼多個位置時，改成整個媒體庫掃描
MAX_PARTIAL_UNITS = 300
IMAGE_FIELDS = ("primary_image", "backdrop_image", "thumb_image", "logo_image")


def image_ext(data: bytes) -> Optional[str]:
    """由檔頭判斷圖片格式（APNG 也是 png）。"""
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"\xff\xd8"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None
QUALITY_JUNK_RE = re.compile(
    r"[\s._-]+(?:2160p|1080p|720p|480p|4k|uhd|bluray|blu-ray|web-?dl|webrip|hdtv|remux|"
    r"x264|x265|h\.?264|h\.?265|hevc|hdr|dv|atmos|dts|aac).*$",
    re.I,
)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def clean_title(stem: str) -> Tuple[str, Optional[int]]:
    """由檔名或資料夾名推出標題與年份。"""
    m = PAREN_YEAR_RE.match(stem)
    if m:
        return m.group("title").strip(" .-_"), int(m.group("year"))
    name = QUALITY_JUNK_RE.sub("", stem)
    year = None
    ym = None
    for ym in YEAR_RE.finditer(name):
        pass
    if ym and ym.start(1) > 0:
        year = int(ym.group(1))
        name = name[: ym.start(1)]
    name = re.sub(r"[._]+", " ", name).strip(" -[]()")
    return name or stem, year


def _number(text: str) -> int:
    """阿拉伯數字或中文數字（十二、二十三、一百零五）。"""
    if text.isdigit():
        return int(text)
    total, cur = 0, 0
    for ch in text:
        if ch == "百":
            total += (cur or 1) * 100
            cur = 0
        elif ch == "十":
            total += (cur or 1) * 10
            cur = 0
        else:
            cur = CN_DIGITS[ch]
    return total + cur


def parse_episode(stem: str) -> Tuple[Optional[int], Optional[int]]:
    for pat in EPISODE_PATTERNS:
        m = pat.search(stem)
        if m:
            gd = m.groupdict()
            season = int(gd["season"]) if gd.get("season") else None
            return season, _number(gd["episode"])
    return None, None


def parse_season_dir(name: str) -> Optional[int]:
    if name.strip().lower() in SPECIALS_DIRS:
        return 0
    for pat in SEASON_DIR_PATTERNS:
        m = pat.match(name.strip())
        if m:
            return _number(m.group("season"))
    return None


def _skip_dir(name: str) -> bool:
    """隱藏資料夾與 NAS 的系統資料夾（@eaDir、#recycle）。"""
    return name.startswith((".", "@", "#"))


def _is_tv(lib: LibraryConfig) -> bool:
    return lib.type.lower() in ("tv", "tvshows", "shows", "series")


def _scope_sql(scope: Path) -> Tuple[str, Tuple]:
    """資料庫裡屬於這個路徑的項目：路徑本身、底下的檔案，以及劇集的「資料夾#季」項目。"""
    s = str(scope)
    n = len(s) + 1
    return "(path=? OR substr(path, 1, ?)=? OR substr(path, 1, ?)=?)", (s, n, s + os.sep, n, s + "#")


def _root_offline(root: Path) -> bool:
    """媒體庫資料夾不存在、讀不到或完全是空的：多半是網路磁碟或共用資料夾還沒掛載好。"""
    try:
        with os.scandir(root) as it:
            return next(it, None) is None
    except OSError:
        return True


def looks_like_series(folder: Path) -> bool:
    """劇集資料夾：有 tvshow.nfo、名稱帶年份（劇名 (2020)）、裡面有季資料夾或直接放著影片。

    其他資料夾（例如「國產劇」「日番」這種分類）不是劇集，要再往下一層找。
    """
    if (folder / "tvshow.nfo").is_file() or PAREN_YEAR_RE.match(folder.name):
        return True
    try:
        entries = list(folder.iterdir())
    except OSError:
        return False
    for entry in entries:
        if entry.is_dir():
            if parse_season_dir(entry.name) is not None:
                return True
        elif entry.suffix.lower() in VIDEO_EXTS:
            return True
    return False


def read_strm(path: Path) -> str:
    """回傳 strm 檔內第一行有效內容（網址或路徑）。"""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def parse_nfo(path: Path) -> Dict:
    if not path or not path.is_file():
        return {}
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return {}

    def text(tag: str) -> Optional[str]:
        el = root.find(tag)
        return el.text.strip() if el is not None and el.text else None

    data: Dict = {}
    if text("title"):
        data["name"] = text("title")
    if text("originaltitle"):
        data["original_title"] = text("originaltitle")
    if text("sorttitle"):
        data["sort_name"] = text("sorttitle")
    if text("plot") or text("outline"):
        data["overview"] = text("plot") or text("outline")
    for tag in ("year",):
        if text(tag) and text(tag).isdigit():
            data["year"] = int(text(tag))
    premiered = text("premiered") or text("aired") or text("releasedate")
    if premiered:
        data["premiere_date"] = premiered[:10] + "T00:00:00.0000000Z"
        if "year" not in data and premiered[:4].isdigit():
            data["year"] = int(premiered[:4])
    rating = text("rating") or text("ratings/rating/value")
    if rating:
        try:
            data["community_rating"] = round(float(rating), 1)
        except ValueError:
            pass
    if text("mpaa"):
        data["official_rating"] = text("mpaa")
    genres = [g.text.strip() for g in root.findall("genre") if g.text]
    if genres:
        data["genres"] = genres
    runtime = text("runtime")
    if runtime and runtime.isdigit():
        data["runtime_ticks"] = int(runtime) * 60 * 10_000_000
    providers: Dict[str, str] = {}
    for uid in root.findall("uniqueid"):
        if uid.text and uid.get("type"):
            providers[uid.get("type").capitalize()] = uid.text.strip()
    for tag, key in (("tmdbid", "Tmdb"), ("imdbid", "Imdb"), ("tvdbid", "Tvdb"), ("imdb_id", "Imdb")):
        if text(tag):
            providers.setdefault(key, text(tag))
    if providers:
        data["provider_ids"] = providers
    for tag, key in (("season", "parent_index_number"), ("episode", "index_number")):
        if text(tag) and text(tag).lstrip("-").isdigit():
            data[key] = int(text(tag))
    return data


def find_image(folder: Path, stems: List[str]) -> Optional[str]:
    for stem in stems:
        for ext in IMAGE_EXTS:
            p = folder / f"{stem}{ext}"
            if p.is_file():
                return str(p)
    return None


class Scanner:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self._lock = threading.Lock()
        self._full_waiting = False
        self.scanning = False
        self.current = ""  # 正在掃描什麼範圍，給網頁顯示
        self.item = ""  # 正在處理哪部片
        self.touched = 0  # 這次已處理的項目數
        # 進度條的分母：上次掃描後資料庫裡這個範圍的項目數（不必先遍歷一次檔案系統）；第一次是 0
        self.expected = 0
        # 透過 API 上傳的圖片（例如 MoviePilot 封面插件做的媒體庫封面），重新掃描時不會被蓋掉
        self.images_dir = config.data_path / "images"
        self.media_info = MediaInfoStore(db)  # 影片旁邊的 X-mediainfo.json
        self._custom: Optional[Dict[Tuple[int, str], str]] = None
        self._custom_lock = threading.Lock()

    # ---- 上傳的圖片 ----
    def _custom_images(self) -> Dict[Tuple[int, str], str]:
        with self._custom_lock:
            if self._custom is None:
                self._custom = {}
                if self.images_dir.is_dir():
                    for f in self.images_dir.iterdir():
                        item_id, _, rest = f.name.partition("-")
                        col = rest.rsplit(".", 1)[0]
                        if item_id.isdigit() and col in IMAGE_FIELDS:
                            self._custom[(int(item_id), col)] = str(f)
            return self._custom

    def custom_image(self, item_id: int, col: str) -> Optional[str]:
        return self._custom_images().get((item_id, col))

    def save_custom_image(self, item_id: int, col: str, data: bytes) -> str:
        ext = image_ext(data)
        if not ext or col not in IMAGE_FIELDS:
            raise ValueError("不支援的圖片格式，請用 jpg、png、gif 或 webp")
        self.remove_custom_image(item_id, col)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        path = self.images_dir / f"{item_id}-{col}.{ext}"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self._custom_images()[(item_id, col)] = str(path)
        self.db.execute(f"UPDATE items SET {col}=? WHERE id=?", (str(path), item_id))
        return str(path)

    def remove_custom_image(self, item_id: int, col: str) -> bool:
        path = self._custom_images().pop((item_id, col), None)
        if path:
            Path(path).unlink(missing_ok=True)
        return bool(path)

    def library_cover(self, lib: LibraryConfig, lib_id: int) -> Optional[str]:
        """媒體庫封面：上傳的優先，其次是媒體庫資料夾裡的 poster／folder／cover 圖。"""
        custom = self.custom_image(lib_id, "primary_image")
        if custom:
            return custom
        for root in lib.paths:
            img = find_image(Path(root).expanduser(), ["poster", "folder", "cover"])
            if img:
                return img
        return None

    # ---- 寫入 ----
    def _upsert(self, path: str, fields: Dict) -> int:
        fields = dict(fields)
        for key in ("genres", "provider_ids"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        fields["seen_scan"] = 1
        if fields.get("type") != "CollectionFolder":
            self.touched += 1
        row = self.db.one("SELECT id FROM items WHERE path=?", (path,))
        if row:
            for col in IMAGE_FIELDS:
                custom = self.custom_image(row["id"], col)
                if custom:
                    fields[col] = custom
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), row["id"]))
            return row["id"]
        fields["path"] = path
        fields.setdefault("date_created", _iso(datetime.now().timestamp()))
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.db.execute(f"INSERT INTO items({cols}) VALUES({marks})", tuple(fields.values()))
        return cur.lastrowid

    # ---- 掃描範圍 ----
    def _begin(self, what: str) -> None:
        self.scanning = True
        self.current = what
        self.item = ""
        self.touched = 0
        self.expected = 0

    def _end(self) -> None:
        self.scanning = False
        self.current = ""
        self.item = ""

    def _count(self, where: str = "1=1", params: Tuple = ()) -> int:
        return self.db.one(f"SELECT COUNT(*) AS c FROM items WHERE type<>'CollectionFolder' AND {where}", params)["c"]

    def _delete_unseen(self, where: str = "1=1", params: Tuple = ()) -> int:
        removed = self.db.execute(f"DELETE FROM items WHERE seen_scan=0 AND {where}", params).rowcount
        if removed:
            self._after_delete()
        return removed

    def _after_delete(self) -> None:
        self.db.execute("DELETE FROM user_data WHERE item_id NOT IN (SELECT id FROM items)")
        self.media_info.prune()
        # 項目刪掉了，它上傳的圖片也刪掉，免得之後新項目用到同一個 id 時誤用
        custom = self._custom_images()
        if custom:
            ids = {r["id"] for r in self.db.query("SELECT id FROM items")}
            for item_id, col in [k for k in custom if k[0] not in ids]:
                self.remove_custom_image(item_id, col)

    def scan_all(self) -> None:
        """掃描全部媒體庫。已經有一次在排隊時不重複排。"""
        if self._full_waiting:
            log.info("已經有一次完整掃描在排隊，略過")
            return
        self._full_waiting = True
        with self._lock:
            self._full_waiting = False
            self._begin("全部媒體庫")
            try:
                self.expected = self._count()
                self.db.execute("UPDATE items SET seen_scan=0")
                for lib in self.config.libraries:
                    self._scan_library(lib)
                removed = self._delete_unseen()
                count = self.db.one("SELECT COUNT(*) AS c FROM items")["c"]
                log.info("掃描完成：共 %s 個項目，移除 %s 個", count, removed)
            finally:
                self._end()

    def scan_libraries(self, names: Iterable[str]) -> None:
        """只掃描指定的媒體庫，並移除設定裡已經沒有的媒體庫。"""
        wanted = set(names)
        with self._lock:
            self._begin("、".join(sorted(wanted)) or "媒體庫")
            try:
                lib_ids = [self._library_item(lib) for lib in self.config.libraries if lib.name in wanted]
                if lib_ids:
                    self.expected = self._count(f"library_id IN ({','.join('?' * len(lib_ids))})", tuple(lib_ids))
                for lib in self.config.libraries:
                    if lib.name not in wanted:
                        continue
                    lib_id = self._library_item(lib)
                    self.db.execute("UPDATE items SET seen_scan=0 WHERE library_id=? AND id<>?", (lib_id, lib_id))
                    self._scan_library(lib)
                    removed = self._delete_unseen("library_id=?", (lib_id,))
                    log.info("掃描媒體庫「%s」完成：%s 個項目，移除 %s 個", lib.name, self.touched, removed)
                self._drop_removed_libraries()
            finally:
                self._end()

    def _drop_removed_libraries(self) -> None:
        keep = {f"library://{lib.name}" for lib in self.config.libraries}
        rows = [r for r in self.db.query("SELECT id, name, path FROM items WHERE type='CollectionFolder'") if r["path"] not in keep]
        for row in rows:
            n = self.db.execute("DELETE FROM items WHERE library_id=? OR id=?", (row["id"], row["id"])).rowcount
            self._after_delete()
            log.info("媒體庫「%s」已從設定移除，刪掉 %s 個項目", row["name"], n)

    def scan_paths(self, paths: Iterable[str]) -> None:
        """只重新掃描這些路徑所在的地方：一部電影、一部劇、一個分類資料夾。

        路徑可以是檔案或資料夾，也可以是已經刪掉的（會把資料庫裡對應的項目移除）。
        媒體庫以外的路徑略過。
        """
        units = self._units(paths)
        offline = {root for _, root, _, _ in units if _root_offline(root)}
        if offline:
            log.warning("媒體庫路徑不存在或是空的，略過：%s", "、".join(sorted(map(str, offline))))
            units = [u for u in units if u[1] not in offline]
        if not units:
            return
        if len(units) > MAX_PARTIAL_UNITS:
            # 變動太多（例如第一次全量同步），逐一處理不如整庫掃
            self.scan_libraries({lib.name for lib, *_ in units})
            return
        with self._lock:
            self._begin(units[0][2].name if len(units) == 1 else f"{len(units)} 個位置")
            try:
                self.expected = sum(self._count(*_scope_sql(scope)) for _, _, scope, _ in units)
                removed = 0
                for lib, root, scope, kind in units:
                    lib_id = self._library_item(lib)
                    where, params = _scope_sql(scope)
                    self.db.execute(f"UPDATE items SET seen_scan=0 WHERE {where}", params)
                    if kind == "series":
                        self._add_series(lib_id, scope)
                    elif kind == "shows":
                        self._scan_shows(lib_id, scope, len(scope.relative_to(root).parts))
                    elif kind == "movies":
                        self._scan_movies(lib_id, root, scope)
                    elif kind == "movie" and scope.is_file():
                        self._add_movie(lib_id, scope, single=False)
                    removed += self._delete_unseen(where, params)
                what = str(units[0][2]) if len(units) == 1 else f"{len(units)} 個位置"
                log.info("掃描 %s 完成：%s 個項目，移除 %s 個", what, self.touched, removed)
            finally:
                self._end()

    def in_library(self, path: str) -> bool:
        return self._where(path) is not None

    def _where(self, raw: str) -> Optional[Tuple[LibraryConfig, Path, Path]]:
        """找出路徑屬於哪個媒體庫的哪個資料夾。"""
        path = Path(os.path.normpath(Path(str(raw)).expanduser()))
        for lib in self.config.libraries:
            for root in lib.paths:
                rp = Path(os.path.normpath(Path(root).expanduser()))
                if path == rp or rp in path.parents:
                    return lib, rp, path
        return None

    def _units(self, paths: Iterable[str]) -> List[Tuple[LibraryConfig, Path, Path, str]]:
        """把路徑換成要重新掃描的單位（去掉重複、被其他單位包含的）。"""
        found: Dict[Path, Tuple[LibraryConfig, Path, Path, str]] = {}
        for raw in paths:
            hit = self._where(raw)
            if not hit:
                continue
            lib, root, path = hit
            unit = self._unit_for_show(root, path) if _is_tv(lib) else self._unit_for_movie(root, path)
            if unit:
                found.setdefault(unit[0], (lib, root, unit[0], unit[1]))
        scopes = sorted(found, key=lambda p: len(p.parts))
        out = []
        for scope in scopes:
            if not any(p == scope or p in scope.parents for p in [u[2] for u in out]):
                out.append(found[scope])
        return out

    @staticmethod
    def _unit_for_movie(root: Path, path: Path) -> Optional[Tuple[Path, str]]:
        """電影：重掃變動所在的資料夾（同資料夾的影片數會影響命名）；
        直接放在媒體庫資料夾底下的檔案只處理那一部。"""
        existing = path
        while not existing.exists() and existing != root:
            existing = existing.parent
        if existing != path:
            if existing != root:
                return existing, "movies"
            # 連同資料夾一起刪掉了：移除資料庫裡這底下的項目
            return root / path.relative_to(root).parts[0], "gone"
        if path.is_dir():
            return path, "movies"
        if path.parent != root:
            return path.parent, "movies"
        if path.suffix.lower() in VIDEO_EXTS:
            return path, "movie"
        # 媒體庫資料夾底下的 nfo、圖片：對應到同名的影片
        videos = sorted(
            (f for f in root.iterdir() if f.suffix.lower() in VIDEO_EXTS and path.stem.startswith(f.stem)),
            key=lambda f: len(f.stem), reverse=True,
        )
        return (videos[0], "movie") if videos else None

    @staticmethod
    def _unit_for_show(root: Path, path: Path) -> Optional[Tuple[Path, str]]:
        """劇集：由媒體庫資料夾往下找，遇到劇集資料夾就重掃整部劇；只到分類資料夾就重掃那個分類。"""
        cur = root
        for depth, part in enumerate(path.relative_to(root).parts):
            nxt = cur / part
            if not nxt.exists():
                # 刪掉的劇或分類：移除資料庫裡這底下的項目
                return nxt, "gone"
            if not nxt.is_dir():
                return None  # 不在任何劇集資料夾裡的檔案
            if looks_like_series(nxt):
                return nxt, "series"
            if depth >= MAX_CATEGORY_DEPTH:
                return None
            cur = nxt
        return cur, "shows"

    def _library_item(self, lib: LibraryConfig) -> int:
        lib_id = self._upsert(
            f"library://{lib.name}",
            {
                "type": "CollectionFolder",
                "collection_type": "tvshows" if _is_tv(lib) else "movies",
                "name": lib.name,
                "sort_name": lib.name.lower(),
                "parent_id": None,
            },
        )
        self.db.execute("UPDATE items SET library_id=? WHERE id=?", (lib_id, lib_id))
        return lib_id

    def _scan_library(self, lib: LibraryConfig) -> None:
        lib_id = self._library_item(lib)
        self.db.execute("UPDATE items SET primary_image=? WHERE id=?", (self.library_cover(lib, lib_id), lib_id))
        for root in lib.paths:
            rp = Path(os.path.normpath(Path(root).expanduser()))
            if _root_offline(rp):
                # 開機時網路磁碟、共用資料夾可能還沒掛載，不能當成檔案全被刪了：保留原本的項目和觀看紀錄
                where, params = _scope_sql(rp)
                kept = self.db.execute(f"UPDATE items SET seen_scan=1 WHERE {where}", params).rowcount
                log.warning("媒體庫路徑不存在或是空的：%s（沒掛載好？）；保留原本的 %s 個項目", rp, kept)
                continue
            if _is_tv(lib):
                self._scan_shows(lib_id, rp)
            else:
                self._scan_movies(lib_id, rp)

    # ---- 電影 ----
    def _scan_movies(self, lib_id: int, root: Path, start: Optional[Path] = None) -> None:
        for dirpath, dirnames, filenames in os.walk(start or root):
            dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
            folder = Path(dirpath)
            videos = sorted(f for f in filenames if Path(f).suffix.lower() in VIDEO_EXTS)
            for fname in videos:
                self._add_movie(lib_id, folder / fname, single=len(videos) == 1 and folder != root)

    def _add_movie(self, lib_id: int, path: Path, single: bool) -> None:
        stem = path.stem
        name, year = clean_title(stem)
        if single:
            # 單片資料夾通常用「片名 (年份)」命名，比檔名乾淨
            dname, dyear = clean_title(path.parent.name)
            if dyear or not year:
                name, year = dname, dyear or year
        self.item = name
        nfo = parse_nfo(path.with_suffix(".nfo"))
        if not nfo and single:
            nfo = parse_nfo(path.parent / "movie.nfo")
        folder = path.parent
        poster_stems = [f"{stem}-poster", stem]
        backdrop_stems = [f"{stem}-fanart", f"{stem}-backdrop"]
        thumb_stems = [f"{stem}-thumb", f"{stem}-landscape"]
        logo_stems = [f"{stem}-logo", f"{stem}-clearlogo"]
        if single:
            poster_stems += ["poster", "folder", "cover"]
            backdrop_stems += ["fanart", "backdrop", "background"]
            thumb_stems += ["thumb", "landscape"]
            logo_stems += ["logo", "clearlogo"]
        st = path.stat()
        fields = {
            "library_id": lib_id,
            "parent_id": lib_id,
            "type": "Movie",
            "name": name,
            "sort_name": name.lower(),
            "year": year,
            "is_strm": int(path.suffix.lower() == ".strm"),
            "container": self._container_for(path),
            "size": None if path.suffix.lower() == ".strm" else st.st_size,
            "primary_image": find_image(folder, poster_stems),
            "backdrop_image": find_image(folder, backdrop_stems),
            "thumb_image": find_image(folder, thumb_stems),
            "logo_image": find_image(folder, logo_stems),
            "date_modified": _iso(st.st_mtime),
        }
        fields.update(nfo)
        if "sort_name" in nfo:
            fields["sort_name"] = nfo["sort_name"].lower()
        elif "name" in nfo:
            fields["sort_name"] = nfo["name"].lower()
        fields.pop("parent_index_number", None)
        fields.pop("index_number", None)
        self._runtime_from_media_info(path, fields)
        self._upsert(str(path), fields)

    def _runtime_from_media_info(self, path: Path, fields: Dict) -> None:
        """讀影片旁邊的 X-mediainfo.json（神醫格式）；nfo 沒有片長時用它的。"""
        info = self.media_info.sync_sidecar(path)
        ticks = info["source"].get("RunTimeTicks") if info else None
        if ticks and not fields.get("runtime_ticks"):
            fields["runtime_ticks"] = int(ticks)

    def _container_for(self, path: Path) -> str:
        if path.suffix.lower() != ".strm":
            return path.suffix.lower().lstrip(".")
        target = read_strm(path)
        ext = Path(target.split("?", 1)[0]).suffix.lower()
        if ext in VIDEO_EXTS and ext != ".strm":
            return ext.lstrip(".")
        # 其他工具把檔名放在 file_name 參數
        m = re.search(r"[?&]file_name=([^&]+)", target)
        if m:
            ext = Path(unquote(m.group(1))).suffix.lower()
            if ext in VIDEO_EXTS and ext != ".strm":
                return ext.lstrip(".")
        return self.config.redirect.default_container

    # ---- 劇集 ----
    def _scan_shows(self, lib_id: int, root: Path, depth: int = 0) -> None:
        """媒體庫路徑底下的每個劇集資料夾各是一部劇；分類資料夾（國產劇、日番…）會往下找。"""
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or _skip_dir(entry.name):
                continue
            if looks_like_series(entry):
                self._add_series(lib_id, entry)
            elif depth < MAX_CATEGORY_DEPTH:
                self._scan_shows(lib_id, entry, depth + 1)

    def _add_series(self, lib_id: int, folder: Path) -> None:
        name, year = clean_title(folder.name)
        self.item = name
        nfo = parse_nfo(folder / "tvshow.nfo")
        nfo.pop("parent_index_number", None)
        nfo.pop("index_number", None)
        st = folder.stat()
        fields = {
            "library_id": lib_id,
            "parent_id": lib_id,
            "type": "Series",
            "name": name,
            "sort_name": name.lower(),
            "year": year,
            "primary_image": find_image(folder, ["poster", "folder", "cover"]),
            "backdrop_image": find_image(folder, ["fanart", "backdrop", "background"]),
            "thumb_image": find_image(folder, ["thumb", "landscape"]),
            "logo_image": find_image(folder, ["logo", "clearlogo"]),
            "date_modified": _iso(st.st_mtime),
        }
        fields.update(nfo)
        if "name" in nfo:
            fields["sort_name"] = nfo.get("sort_name", nfo["name"]).lower()
        series_id = self._upsert(str(folder), fields)

        episodes: List[Tuple[Path, Optional[int]]] = []
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames.sort()
            d = Path(dirpath)
            dir_season = parse_season_dir(d.name) if d != folder else None
            for f in sorted(filenames):
                if Path(f).suffix.lower() in VIDEO_EXTS:
                    episodes.append((d / f, dir_season))

        seasons: Dict[int, int] = {}
        latest = None
        for path, dir_season in episodes:
            s, e = parse_episode(path.stem)
            ep_nfo = parse_nfo(path.with_suffix(".nfo"))
            season_no = ep_nfo.get("parent_index_number", dir_season if dir_season is not None else s)
            if season_no is None:
                season_no = 1
            if season_no not in seasons:
                seasons[season_no] = self._add_season(lib_id, series_id, folder, path.parent, season_no)
            season_id = seasons[season_no]
            ep_no = ep_nfo.get("index_number", e)
            ep_name = ep_nfo.get("name") or (f"第 {ep_no} 集" if ep_no is not None else path.stem)
            st = path.stat()
            efields = {
                "library_id": lib_id,
                "parent_id": season_id,
                "type": "Episode",
                "name": ep_name,
                "sort_name": f"{season_no:04d}-{(ep_no or 0):05d}-{path.stem.lower()}",
                "is_strm": int(path.suffix.lower() == ".strm"),
                "container": self._container_for(path),
                "size": None if path.suffix.lower() == ".strm" else st.st_size,
                "index_number": ep_no,
                "parent_index_number": season_no,
                "series_id": series_id,
                "season_id": season_id,
                "primary_image": find_image(path.parent, [f"{path.stem}-thumb", path.stem]),
                "date_modified": _iso(st.st_mtime),
            }
            ep_nfo.pop("name", None)
            ep_nfo.pop("sort_name", None)
            efields.update(ep_nfo)
            efields["parent_index_number"] = season_no
            self._runtime_from_media_info(path, efields)
            self._upsert(str(path), efields)
            latest = max(latest or st.st_mtime, st.st_mtime)
        if latest:
            self.db.execute("UPDATE items SET date_modified=? WHERE id=?", (_iso(latest), series_id))

    def _add_season(self, lib_id: int, series_id: int, series_dir: Path, ep_dir: Path, season_no: int) -> int:
        season_dir = ep_dir if ep_dir != series_dir and parse_season_dir(ep_dir.name) == season_no else None
        key = str(season_dir) if season_dir else f"{series_dir}#season{season_no}"
        name = "特別篇" if season_no == 0 else f"第 {season_no} 季"
        nfo = parse_nfo(season_dir / "season.nfo") if season_dir else {}
        tag = "specials" if season_no == 0 else f"{season_no:02d}"
        primary = find_image(series_dir, [f"season{tag}-poster", f"season{tag}"])
        if not primary and season_dir:
            primary = find_image(season_dir, ["poster", "folder", "cover"])
        fields = {
            "library_id": lib_id,
            "parent_id": series_id,
            "type": "Season",
            "name": nfo.get("name") or name,
            "sort_name": f"{season_no:04d}",
            "index_number": season_no,
            "series_id": series_id,
            "primary_image": primary,
            "overview": nfo.get("overview"),
        }
        return self._upsert(key, fields)
