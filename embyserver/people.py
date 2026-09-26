"""演職人員：從 nfo 讀演員、導演、編劇，給播放器顯示；查中文名；類型中文化。

- nfo 的 <actor>（name、role、thumb、tmdbid、type）、<director tmdbid="…">、<credits>（編劇）
  存進 people 表，每個項目一組，項目更新時整批重寫。
- 人物 id：有 TMDB id 用 p{tmdbid}，沒有用名稱雜湊 pn{…}，播放器點進去看作品、看頭像都靠它。
- 中文名：TMDB 的人名常是拼音或英文（Chen He）。先問 MoviePilot 的人物介面，從 TMDB 別名裡挑中文的
  （陈赫）；沒有再批次問 Wikidata（P4985 = TMDB 人物 id）。查過沒有的也記下，30 天後再查。
- 類型：英文的 TMDB 類型換成中文，繁體換成簡體，去掉重複。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Dict, Iterable, List, Optional, Tuple

import httpx

from .db import Database
from .textutil import has_cjk, simplified

log = logging.getLogger(__name__)

WIKIDATA_API = "https://query.wikidata.org/sparql"
WIKIDATA_UA = "Mi302/0.1 (https://github.com/MiCat-S/Mi302)"
WIKIDATA_LANGS = ("zh-hans", "zh-cn", "zh-sg", "zh", "zh-hant", "zh-tw", "zh-hk")
RECHECK_SECONDS = 30 * 86400
TOP_ACTORS = 20  # 每部作品只查前幾位演員的中文名（加上導演、編劇）
KANA_RE = re.compile(r"[぀-ヿ]")

# TMDB 電影、劇集類型（和常見的 IMDb 類型）的英文名 → 中文（TMDB 簡體中文的叫法）
GENRES_ZH = {
    "action": "动作", "adventure": "冒险", "animation": "动画", "comedy": "喜剧", "crime": "犯罪",
    "documentary": "纪录", "drama": "剧情", "family": "家庭", "fantasy": "奇幻", "history": "历史",
    "horror": "恐怖", "music": "音乐", "mystery": "悬疑", "romance": "爱情", "science fiction": "科幻",
    "sci-fi": "科幻", "tv movie": "电视电影", "thriller": "惊悚", "war": "战争", "western": "西部",
    "action & adventure": "动作冒险", "kids": "儿童", "news": "新闻", "reality": "真人秀",
    "sci-fi & fantasy": "科幻奇幻", "soap": "肥皂剧", "talk": "脱口秀", "war & politics": "战争政治",
    "biography": "传记", "sport": "运动", "sports": "运动", "musical": "歌舞", "film-noir": "黑色电影",
    "short": "短片", "game-show": "游戏节目", "reality-tv": "真人秀", "talk-show": "脱口秀", "anime": "动画",
}


def localize_genres(genres: Iterable[str]) -> List[str]:
    """英文類型換成中文、繁體換成簡體，保留順序、去掉重複。"""
    out: List[str] = []
    for g in genres or []:
        g = (g or "").strip()
        if not g:
            continue
        zh = GENRES_ZH.get(g.lower()) or (simplified(g) if has_cjk(g) else g)
        if zh not in out:
            out.append(zh)
    return out


# ---------------- nfo ----------------


def parse_people(root: ET.Element) -> List[dict]:
    """nfo 裡的演職人員，依出現順序：導演、編劇、演員。"""
    out: List[dict] = []

    def add(name: Optional[str], kind: str, role: str = "", tmdbid: str = "", thumb: str = "") -> None:
        name = (name or "").strip()
        if name:
            out.append({"name": name, "type": kind, "role": role.strip(), "tmdbid": tmdbid.strip(), "thumb": thumb.strip()})

    for el in root.findall("director"):
        add(el.text, "Director", tmdbid=el.get("tmdbid") or "")
    for tag in ("credits", "writer"):
        for el in root.findall(tag):
            add(el.text, "Writer", tmdbid=el.get("tmdbid") or "")
    for el in root.findall("actor"):
        def sub(tag: str) -> str:
            node = el.find(tag)
            return node.text.strip() if node is not None and node.text else ""

        kind = sub("type")
        add(sub("name"), "GuestStar" if kind.lower() == "gueststar" else "Actor",
            role=sub("role"), tmdbid=sub("tmdbid"), thumb=sub("thumb"))
    return out


def person_id(tmdbid: Optional[str], name: str) -> str:
    if tmdbid and str(tmdbid).isdigit():
        return f"p{tmdbid}"
    return "pn" + hashlib.md5(name.strip().lower().encode()).hexdigest()[:12]


def image_tag(url: Optional[str]) -> Optional[str]:
    return hashlib.md5(url.encode()).hexdigest() if url else None


class PeopleStore:
    def __init__(self, db: Database, config=None):
        self.db = db
        self.config = config

    @property
    def chinese(self) -> bool:
        return bool(self.config is None or self.config.server.chinese_people)

    def set(self, item_id: int, people: List[dict]) -> None:
        rows = [
            (item_id, i, p["name"], p.get("role") or None, p["type"], p.get("tmdbid") or None,
             p.get("thumb") or None, person_id(p.get("tmdbid"), p["name"]))
            for i, p in enumerate(people)
        ]
        with self.db.lock:
            self.db.conn.execute("DELETE FROM people WHERE item_id=?", (item_id,))
            if rows:
                self.db.conn.executemany(
                    "INSERT INTO people(item_id, ord, name, role, type, tmdbid, thumb, pid) VALUES(?,?,?,?,?,?,?,?)", rows
                )
            self.db.conn.commit()

    def prune(self) -> None:
        self.db.execute("DELETE FROM people WHERE item_id NOT IN (SELECT id FROM items)")

    def _display(self, name: str, zh: Optional[str]) -> str:
        return zh if self.chinese and zh else name

    def for_item(self, item) -> List[dict]:
        """項目的演職人員（Emby 的 BaseItemPerson）；集沒有自己的就用劇的。"""
        rows = self._rows(item["id"])
        if not rows and item["type"] in ("Episode", "Season") and item["series_id"]:
            rows = self._rows(item["series_id"])
        out = []
        for r in rows:
            p = {"Name": self._display(r["name"], r["zh"]), "Id": r["pid"], "Type": r["type"]}
            if r["role"]:
                p["Role"] = r["role"]
            if r["thumb"]:
                p["PrimaryImageTag"] = image_tag(r["thumb"])
            out.append(p)
        return out

    def _rows(self, item_id: int):
        return self.db.query(
            "SELECT p.*, n.zh FROM people p LEFT JOIN person_names n ON n.tmdbid=p.tmdbid "
            "WHERE p.item_id=? ORDER BY p.ord", (item_id,),
        )

    def person(self, pid: str) -> Optional[dict]:
        row = self.db.one(
            "SELECT p.name, p.tmdbid, p.thumb, n.zh FROM people p LEFT JOIN person_names n ON n.tmdbid=p.tmdbid "
            "WHERE p.pid=? ORDER BY p.thumb IS NULL LIMIT 1", (pid.lower(),),
        )
        return dict(row) if row else None

    def by_name(self, name: str) -> Optional[str]:
        """名稱（原名或中文名）→ 人物 id。"""
        row = self.db.one(
            "SELECT p.pid FROM people p LEFT JOIN person_names n ON n.tmdbid=p.tmdbid "
            "WHERE lower(p.name)=lower(?) OR n.zh=? LIMIT 1", (name, simplified(name)),
        )
        return row["pid"] if row else None

    def person_dto(self, pid: str, server_id: str) -> Optional[dict]:
        p = self.person(pid)
        if not p:
            return None
        dto = {"Name": self._display(p["name"], p["zh"]), "Id": pid.lower(), "ServerId": server_id, "Type": "Person",
               "IsFolder": False, "ImageTags": {}}
        if p["zh"] and self.chinese and p["zh"] != p["name"]:
            dto["OriginalTitle"] = p["name"]
        if p["thumb"]:
            dto["ImageTags"]["Primary"] = image_tag(p["thumb"])
        if p["tmdbid"]:
            dto["ProviderIds"] = {"Tmdb": p["tmdbid"]}
        return dto

    def item_ids(self, pids: Iterable[str]) -> List[int]:
        pids = [p.lower() for p in pids if p]
        if not pids:
            return []
        marks = ",".join("?" for _ in pids)
        return [r["item_id"] for r in self.db.query(f"SELECT DISTINCT item_id FROM people WHERE pid IN ({marks})", pids)]

    def search(self, term: str, limit: int = 50) -> List[str]:
        """原名、中文名都認，回傳人物 id；出現在越多作品的排越前面。"""
        like = f"%{term.strip()}%"
        rows = self.db.query(
            "SELECT p.pid, COUNT(DISTINCT p.item_id) AS c FROM people p LEFT JOIN person_names n ON n.tmdbid=p.tmdbid "
            "WHERE p.name LIKE ? OR n.zh LIKE ? GROUP BY p.pid ORDER BY c DESC LIMIT ?",
            (like, f"%{simplified(term.strip())}%", limit),
        )
        return [r["pid"] for r in rows]


# ---------------- 中文名 ----------------


def pick_chinese(aliases: Iterable[str], name: Optional[str] = None) -> Optional[str]:
    """從 TMDB 的別名裡挑中文名：去掉日文假名的，優先本來就是簡體的，都沒有就把繁體轉成簡體。"""
    cands = [a.strip() for a in [name or "", *(aliases or [])] if a and has_cjk(a) and not KANA_RE.search(a)]
    for c in cands:
        if simplified(c) == c:
            return c
    return simplified(cands[0]) if cands else None


class PersonNames:
    """背景查演職人員的中文名：先問 MoviePilot（TMDB 別名），沒有再問 Wikidata。"""

    def __init__(self, db: Database, config, moviepilot, transport: Optional[httpx.BaseTransport] = None):
        self.db = db
        self.config = config
        self.moviepilot = moviepilot
        self._transport = transport
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.running = False
        self.last_error = ""
        self.last_run = 0.0
        self.pause = 0.5  # 問 MoviePilot 的間隔（每秒最多 2 次，它要轉問 TMDB）

    def pending(self, limit: int = 500) -> List[str]:
        """還沒查過、或查過沒有而且超過 30 天的 TMDB 人物 id。"""
        rows = self.db.query(
            "SELECT DISTINCT p.tmdbid FROM people p LEFT JOIN person_names n ON n.tmdbid=p.tmdbid "
            "WHERE p.tmdbid IS NOT NULL AND (p.type<>'Actor' OR p.ord<?) "
            "AND (n.tmdbid IS NULL OR (n.zh IS NULL AND n.at<?)) LIMIT ?",
            (TOP_ACTORS + 10, int(time.time() - RECHECK_SECONDS), limit),
        )
        return [r["tmdbid"] for r in rows]

    def status(self) -> dict:
        one = lambda sql: self.db.one(sql)["c"]  # noqa: E731
        return {
            "people": one("SELECT COUNT(DISTINCT pid) AS c FROM people"),
            "resolved": one("SELECT COUNT(*) AS c FROM person_names WHERE zh IS NOT NULL"),
            "none": one("SELECT COUNT(*) AS c FROM person_names WHERE zh IS NULL"),
            "from_moviepilot": one("SELECT COUNT(*) AS c FROM person_names WHERE source='moviepilot'"),
            "from_wikidata": one("SELECT COUNT(*) AS c FROM person_names WHERE source='wikidata'"),
            "pending": len(self.pending(100000)),
            "running": self.running, "last_error": self.last_error, "last_run": int(self.last_run) or None,
            "moviepilot": bool(self.moviepilot and self.moviepilot.enabled),
        }

    # -- 兩個來源 --

    def _from_moviepilot(self, tmdbid: str) -> Tuple[bool, Optional[str]]:
        """(問到了沒有, 中文名)。"""
        from .moviepilot import MoviePilotError

        try:
            body = self.moviepilot._request("GET", f"/api/v1/tmdb/person/{tmdbid}", timeout=20)
        except MoviePilotError as exc:
            self.last_error = f"MoviePilot：{exc}"
            return False, None
        data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
        if not isinstance(data, dict):
            return True, None
        return True, pick_chinese(data.get("also_known_as") or [], data.get("name"))

    def _from_wikidata(self, ids: List[str]) -> Optional[Dict[str, str]]:
        """批次查；連不上回傳 None（這次當作沒問到）。"""
        values = " ".join(f'"{i}"' for i in ids if i.isdigit())
        langs = ", ".join(f'"{lang}"' for lang in WIKIDATA_LANGS)
        query = (
            "SELECT ?tmdb ?lang ?label WHERE { VALUES ?tmdb { %s } ?p wdt:P4985 ?tmdb . ?p rdfs:label ?label . "
            "BIND(LANG(?label) AS ?lang) FILTER(?lang IN (%s)) }" % (values, langs)
        )
        try:
            with httpx.Client(timeout=30, transport=self._transport, headers={"User-Agent": WIKIDATA_UA}) as client:
                resp = client.post(WIKIDATA_API, data={"query": query, "format": "json"},
                                   headers={"Accept": "application/sparql-results+json"})
                resp.raise_for_status()
                rows = resp.json()["results"]["bindings"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            self.last_error = f"Wikidata：{exc}"
            return None
        best: Dict[str, Tuple[int, str]] = {}
        for r in rows:
            tmdb, lang, label = r["tmdb"]["value"], r["lang"]["value"], r["label"]["value"]
            rank = WIKIDATA_LANGS.index(lang) if lang in WIKIDATA_LANGS else 99
            if has_cjk(label) and (tmdb not in best or rank < best[tmdb][0]):
                best[tmdb] = (rank, simplified(label))
        return {k: v[1] for k, v in best.items()}

    # -- 執行 --

    def run(self, limit: int = 500) -> int:
        """查一批，回傳這次寫進去幾個。"""
        if not self._lock.acquire(blocking=False):
            return 0
        self.running = True
        written = 0
        try:
            ids = self.pending(limit)
            if not ids:
                return 0
            found: Dict[str, Tuple[str, str]] = {}
            asked_mp = set()
            use_mp = bool(self.moviepilot and self.moviepilot.enabled)
            for tmdbid in ids:
                if not use_mp or self._stop.is_set():
                    break
                ok, zh = self._from_moviepilot(tmdbid)
                if not ok:
                    use_mp = False  # MoviePilot 連不上，這次剩下的改問 Wikidata
                    break
                asked_mp.add(tmdbid)
                if zh:
                    found[tmdbid] = (zh, "moviepilot")
                time.sleep(self.pause)
            asked_wd = set()
            misses = [i for i in ids if i not in found]
            for start in range(0, len(misses), 100):
                chunk = misses[start:start + 100]
                got = self._from_wikidata(chunk)
                if got is None:
                    break
                asked_wd.update(chunk)
                for tmdbid, zh in got.items():
                    found.setdefault(tmdbid, (zh, "wikidata"))
            now = int(time.time())
            mp_configured = bool(self.moviepilot and self.moviepilot.enabled)
            rows = []
            for tmdbid in ids:
                if tmdbid in found:
                    rows.append((tmdbid, found[tmdbid][0], found[tmdbid][1], now))
                elif tmdbid in asked_wd and (tmdbid in asked_mp or not mp_configured):
                    rows.append((tmdbid, None, "none", now))  # 兩邊都問過都沒有，30 天後再查
            self.db.executemany(
                "INSERT INTO person_names(tmdbid, zh, source, at) VALUES(?,?,?,?) "
                "ON CONFLICT(tmdbid) DO UPDATE SET zh=excluded.zh, source=excluded.source, at=excluded.at", rows,
            )
            written = len(rows)
            log.info("演職人員中文名：查了 %s 位，找到 %s 位", len(ids), len(found))
            return written
        finally:
            self.running = False
            self.last_run = time.time()
            self._lock.release()

    def run_in_background(self) -> bool:
        if self._lock.locked():
            return False
        threading.Thread(target=self.run, daemon=True).start()
        return True

    def start(self) -> None:
        """每 30 分鐘看一次有沒有新的人要查（掃描、同步後新增的作品）。"""

        def loop():
            while not self._stop.wait(60):
                if self.config.server.chinese_people and self.pending(1):
                    try:
                        self.run()
                    except Exception:
                        log.exception("查演職人員中文名失敗")
                if self._stop.wait(1740):
                    return

        threading.Thread(target=loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
