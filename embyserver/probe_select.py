"""挑選要探測媒體資訊的影片：不一定整庫都做，可以篩選、決定先做哪些、這次做幾支。

候選是媒體庫裡還沒有媒體資訊的電影和集（資料庫 media_info 沒有那一列）。劇集以「劇」為單位列出
（網頁上一部劇一行），篩選和排序看劇本身的年份、評分、片名；電影就看自己。

- 篩選：搜尋片名（原名、簡繁體、拼音全拼、首字母都認）、媒體庫、電影或劇集、年份範圍。
- 先做哪些：最近加入的、年份新的、年份舊的、評分高的，或照片名。同一部劇裡照季、集順序。
- 探測前 prober 還會再確認一次（旁邊已經有 X-mediainfo.json 的會略過）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Mapping, Optional, Tuple

from .db import Database
from .textutil import cjk_count, pinyin_full, simplified

# 先做哪些：(網頁上的名稱, 片名清單的排序, 探測順序)。v 的欄位見 _VIDEOS
ORDERS = {
    "added": ("最近加入的先做", "added DESC, sort_name, title_id", "added DESC, sort_name, title_id, season, episode"),
    "year_desc": ("年份新的先做", "year IS NULL, year DESC, sort_name, title_id",
                  "year IS NULL, year DESC, sort_name, title_id, season, episode"),
    "year_asc": ("年份舊的先做", "year IS NULL, year ASC, sort_name, title_id",
                 "year IS NULL, year ASC, sort_name, title_id, season, episode"),
    "rating": ("評分高的先做", "rating IS NULL, rating DESC, sort_name, title_id",
               "rating IS NULL, rating DESC, sort_name, title_id, season, episode"),
    "name": ("照片名", "sort_name, title_id", "sort_name, title_id, season, episode"),
}
DEFAULT_ORDER = "added"
KINDS = {"movie": "Movie", "series": "Episode"}  # 網頁上的「電影」「劇集」→ 影片的類型

# 每一支影片（電影或集），加上它所屬的「片名」：集用劇的名稱、年份、評分，電影用自己的
_VIDEOS = """
WITH v AS (
    SELECT i.path, i.type, i.library_id, i.date_created AS added,
           i.parent_index_number AS season, i.index_number AS episode,
           COALESCE(i.series_id, i.id) AS title_id,
           COALESCE(s.name, i.name) AS title,
           COALESCE(s.original_title, i.original_title) AS original_title,
           COALESCE(s.search_text, i.search_text) AS search_text,
           COALESCE(s.sort_name, i.sort_name) AS sort_name,
           COALESCE(s.year, i.year) AS year,
           COALESCE(s.community_rating, i.community_rating) AS rating,
           (m.path IS NULL) AS missing
    FROM items i
    LEFT JOIN items s ON s.id = i.series_id
    LEFT JOIN media_info m ON m.path = i.path
    WHERE i.type IN ('Movie', 'Episode')
)
"""


def _int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass
class ProbeFilter:
    """網頁上的篩選條件；沒填的條件不限制。"""

    query: str = ""
    library_id: Optional[int] = None
    kind: str = ""  # "" = 電影和劇集、movie、series
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    order: str = DEFAULT_ORDER

    @classmethod
    def from_params(cls, raw: Mapping) -> "ProbeFilter":
        """網址查詢參數或 JSON 內容轉成篩選條件；看不懂的值當成沒填。"""
        kind = str(raw.get("kind") or "")
        order = str(raw.get("order") or DEFAULT_ORDER)
        year_from, year_to = _int(raw.get("year_from")), _int(raw.get("year_to"))
        if year_from and year_to and year_from > year_to:
            year_from, year_to = year_to, year_from  # 填反了就對調
        return cls(
            query=str(raw.get("q") or "").strip(),
            library_id=_int(raw.get("library")),
            kind=kind if kind in KINDS else "",
            year_from=year_from,
            year_to=year_to,
            order=order if order in ORDERS else DEFAULT_ORDER,
        )

    def where(self) -> Tuple[str, list]:
        conds, params = ["1=1"], []
        if self.kind:
            conds.append("type = ?")
            params.append(KINDS[self.kind])
        if self.library_id:
            conds.append("library_id = ?")
            params.append(self.library_id)
        if self.year_from:
            conds.append("year >= ?")
            params.append(self.year_from)
        if self.year_to:
            conds.append("year <= ?")
            params.append(self.year_to)
        if self.query:
            # 和播放器的搜尋一樣：原名、簡體、全拼、首字母；兩個字以上的中文再用拼音比
            like = ["title LIKE ?", "original_title LIKE ?", "search_text LIKE ?"]
            params += [f"%{self.query}%", f"%{self.query}%", f"%{simplified(self.query).lower()}%"]
            if cjk_count(self.query) >= 2:
                like.append("search_text LIKE ?")
                params.append(f"%{pinyin_full(self.query)}%")
            conds.append("(" + " OR ".join(like) + ")")
        return " AND ".join(conds), params

    def describe(self) -> str:
        """給結果顯示的簡短說明，例如「劇集・2019–2023・最近加入的先做」。"""
        parts = []
        if self.query:
            parts.append(f"搜尋「{self.query}」")
        if self.kind:
            parts.append("電影" if self.kind == "movie" else "劇集")
        if self.year_from or self.year_to:
            parts.append(f"{self.year_from or ''}–{self.year_to or ''}")
        parts.append(ORDERS[self.order][0])
        return "・".join(parts)


def missing_titles(db: Database, flt: ProbeFilter, limit: int = 20, offset: int = 0) -> dict:
    """還缺媒體資訊的片名（電影一部一行、劇一部一行），照「先做哪些」排好，分頁。

    回傳 items（每一行：id、type、name、year、library_id、missing 缺幾支、total 共幾支）、
    total（符合的片名數）、movies、episodes（符合條件還缺的電影、集數）。
    """
    where, params = flt.where()
    grouped = (
        f"{_VIDEOS} SELECT title_id AS id, MIN(type) AS type, MIN(title) AS name, MIN(year) AS year, "
        "MIN(library_id) AS library_id, SUM(missing) AS missing, COUNT(*) AS total, "
        "MAX(CASE WHEN missing THEN added END) AS added, MIN(rating) AS rating, MIN(sort_name) AS sort_name "
        f"FROM v WHERE {where} GROUP BY title_id HAVING SUM(missing) > 0"
    )
    rows = db.query(f"{grouped} ORDER BY {ORDERS[flt.order][1]} LIMIT ? OFFSET ?", (*params, limit, offset))
    total = db.one(f"SELECT COUNT(*) AS c FROM ({grouped})", params)["c"]
    counts = db.one(
        f"{_VIDEOS} SELECT COALESCE(SUM(type = 'Movie'), 0) AS movies, COALESCE(SUM(type = 'Episode'), 0) AS episodes "
        f"FROM v WHERE missing AND {where}", params,
    )
    return {
        "items": [
            {"id": r["id"], "type": "Movie" if r["type"] == "Movie" else "Series", "name": r["name"], "year": r["year"],
             "library_id": r["library_id"], "missing": r["missing"], "total": r["total"]}
            for r in rows
        ],
        "total": total,
        "movies": counts["movies"],
        "episodes": counts["episodes"],
    }


def missing_paths(db: Database, flt: ProbeFilter, limit: int = 0, title_ids: Optional[Iterable[int]] = None) -> List[str]:
    """照「先做哪些」排好的影片路徑；limit = 0 表示全部。title_ids 只取這幾部電影或劇。"""
    where, params = flt.where()
    ids = [int(i) for i in title_ids or []]
    if ids:
        where += f" AND title_id IN ({','.join('?' for _ in ids)})"
        params += ids
    sql = f"{_VIDEOS} SELECT path FROM v WHERE missing AND {where} ORDER BY {ORDERS[flt.order][2]}"
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return [r["path"] for r in db.query(sql, params)]


def title_names(db: Database, ids: Iterable[int], limit: int = 3) -> str:
    """幾部片名接成一串，給結果說明用：「庆余年」、「流浪地球」等 5 部。"""
    ids = [int(i) for i in ids]
    if not ids:
        return ""
    rows = db.query(f"SELECT name FROM items WHERE id IN ({','.join('?' for _ in ids)}) ORDER BY sort_name LIMIT ?",
                    (*ids, limit))
    names = "、".join(f"「{r['name']}」" for r in rows)
    return names + (f"等 {len(ids)} 部" if len(ids) > limit else "")
