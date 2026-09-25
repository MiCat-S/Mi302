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
from typing import Dict, List, Optional, Tuple

from .config import Config, LibraryConfig
from .db import Database

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
    re.compile(r"第\s*(?P<episode>\d{1,4})\s*[集话話]"),
    re.compile(r"(?:^|[\s._\-\[])[Ee][Pp]?(?P<episode>\d{1,4})(?!\d)"),
]
SEASON_DIR_PATTERNS = [
    re.compile(r"^(?:season|series)[\s._-]*(?P<season>\d{1,3})$", re.I),
    re.compile(r"^s(?P<season>\d{1,3})$", re.I),
    re.compile(r"^第\s*(?P<season>\d{1,3})\s*季$"),
]
SPECIALS_DIRS = {"specials", "special", "sp", "特别篇", "特別篇"}
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


def parse_episode(stem: str) -> Tuple[Optional[int], Optional[int]]:
    for pat in EPISODE_PATTERNS:
        m = pat.search(stem)
        if m:
            gd = m.groupdict()
            season = int(gd["season"]) if gd.get("season") else None
            return season, int(gd["episode"])
    return None, None


def parse_season_dir(name: str) -> Optional[int]:
    if name.strip().lower() in SPECIALS_DIRS:
        return 0
    for pat in SEASON_DIR_PATTERNS:
        m = pat.match(name.strip())
        if m:
            return int(m.group("season"))
    return None


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
        self.scanning = False

    # ---- 寫入 ----
    def _upsert(self, path: str, fields: Dict) -> int:
        fields = dict(fields)
        for key in ("genres", "provider_ids"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        fields["seen_scan"] = 1
        row = self.db.one("SELECT id FROM items WHERE path=?", (path,))
        if row:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), row["id"]))
            return row["id"]
        fields["path"] = path
        fields.setdefault("date_created", _iso(datetime.now().timestamp()))
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.db.execute(f"INSERT INTO items({cols}) VALUES({marks})", tuple(fields.values()))
        return cur.lastrowid

    def scan_all(self) -> None:
        if not self._lock.acquire(blocking=False):
            log.info("已有掃描在進行，略過")
            return
        self.scanning = True
        try:
            self.db.execute("UPDATE items SET seen_scan=0")
            for lib in self.config.libraries:
                self._scan_library(lib)
            removed = self.db.execute("DELETE FROM items WHERE seen_scan=0").rowcount
            self.db.execute(
                "DELETE FROM user_data WHERE item_id NOT IN (SELECT id FROM items)"
            )
            count = self.db.one("SELECT COUNT(*) AS c FROM items")["c"]
            log.info("掃描完成：共 %s 個項目，移除 %s 個", count, removed)
        finally:
            self.scanning = False
            self._lock.release()

    def _scan_library(self, lib: LibraryConfig) -> None:
        ctype = "tvshows" if lib.type.lower() in ("tv", "tvshows", "shows", "series") else "movies"
        lib_id = self._upsert(
            f"library://{lib.name}",
            {
                "type": "CollectionFolder",
                "collection_type": ctype,
                "name": lib.name,
                "sort_name": lib.name.lower(),
                "parent_id": None,
            },
        )
        self.db.execute("UPDATE items SET library_id=? WHERE id=?", (lib_id, lib_id))
        # 取第一個路徑下的 poster/folder 當媒體庫封面
        for root in lib.paths:
            img = find_image(Path(root), ["poster", "folder", "cover"])
            if img:
                self.db.execute("UPDATE items SET primary_image=? WHERE id=?", (img, lib_id))
                break
        for root in lib.paths:
            rp = Path(root).expanduser()
            if not rp.is_dir():
                log.warning("媒體庫路徑不存在：%s", rp)
                continue
            if ctype == "movies":
                self._scan_movies(lib_id, rp)
            else:
                self._scan_shows(lib_id, rp)

    # ---- 電影 ----
    def _scan_movies(self, lib_id: int, root: Path) -> None:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
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
        self._upsert(str(path), fields)

    def _container_for(self, path: Path) -> str:
        if path.suffix.lower() != ".strm":
            return path.suffix.lower().lstrip(".")
        target = read_strm(path)
        ext = Path(target.split("?", 1)[0]).suffix.lower()
        if ext in VIDEO_EXTS and ext != ".strm":
            return ext.lstrip(".")
        return self.config.redirect.default_container

    # ---- 劇集 ----
    def _scan_shows(self, lib_id: int, root: Path) -> None:
        for entry in sorted(root.iterdir()):
            if entry.is_dir():
                self._add_series(lib_id, entry)

    def _add_series(self, lib_id: int, folder: Path) -> None:
        name, year = clean_title(folder.name)
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
