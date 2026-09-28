"""片頭片尾：從播放行為學出來，給播放器「跳過片頭」「跳過片尾」用。

不碰 115、不讀影片：Mi302 本來就會收到播放器的進度回報（每幾秒一次的位置），從裡面看得出
「在開頭跳過了一段」和「片尾停下來、切下一集」。做法參考 Emby 神醫助手的「片頭探測 ‐ 播放行為」。

- 片頭：開頭 10 分鐘內（短的集是前 25%），位置往前跳了 15 秒到 3 分鐘、而且跳得比實際經過的時間多很多
  （不是倍速播放），就記下「從哪跳到哪」。只看跳的那一下，之後怎麼播不管。
  同一季的其他集沒有自己的紀錄時，套用這一季所有紀錄的中位數。
- 片尾：最後 5 分鐘（短的集是最後 25%）裡停下（切下一集、關掉）但沒播完，或往前跳了 60 秒以上、跳到結尾，
  記下位置；同一季套用「距離結尾多久」的中位數。
- 每個使用者對每一集只留最後一次紀錄，多人多集時取中位數，偶爾亂跳不會蓋掉。

也可以在網頁上手動設定某一季的片頭、片尾（或標成這一季沒有）；設了的部分不用學到的值，學的紀錄照留，
改回「自動」就恢復。

給播放器的格式（三種都給，播放器認哪種用哪種）：
- Emby：項目的 Chapters 裡加 MarkerType 為 IntroStart／IntroEnd／CreditsStart 的章節。
- Jellyfin Intro Skipper 外掛：GET /Episode/{id}/IntroTimestamps（/v1）、/Episode/{id}/Timestamps。
- Jellyfin 10.10 的媒體片段：GET /MediaSegments/{id}。
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .db import Database
from .textutil import title_match

log = logging.getLogger(__name__)

TICK = 10_000_000  # 一秒
# 範圍是查過資料訂的：Emby、Intro Skipper 都掃前 10 分鐘；美劇冷開場最長到 9 分多；動畫 OP 前有冷開場的很常見，
# 3 分鐘後才進 OP 的佔一成；動畫 ED 加預告 99.8% 在最後 5 分鐘內；陸劇規範片頭最多 90 秒、片尾加預告最多 3.5 分鐘。
INTRO_WINDOW = 10 * 60 * TICK  # 片頭最晚從開頭 10 分鐘內開始跳
MAX_INTRO_TICKS = 180 * TICK  # 一次跳過的長度上限：片頭 90 秒加前情提要、重複片段（廣電規範合計最多 150 秒）
MIN_JUMP_TICKS = 15 * TICK  # 往前跳至少 15 秒才算跳片頭
CREDITS_WINDOW = 5 * 60 * TICK  # 片尾區：最後 5 分鐘
MIN_CREDITS_JUMP = 60 * TICK  # 片尾區裡往前跳 60 秒以上：跳過 ED（後面可能還有預告，不一定跳到結尾）
MIN_CREDITS_TICKS = 20 * TICK  # 距離結尾至少 20 秒才算片尾（不然是播完了）
ZONE = 0.25  # 短的集改用前 25%、後 25%：24 分鐘的動畫是前 6 分鐘、後 5 分鐘
MODES = ("auto", "manual", "none")  # 手動設定：照學的、用設定的值、這一季沒有
MAX_MANUAL_TICKS = 60 * 60 * TICK  # 手動設定的時間上限：片頭結束、片尾長度都不超過一小時
SESSION_TTL = 6 * 3600
MAX_SESSIONS = 2000


def windows(runtime: int) -> Tuple[int, Optional[int]]:
    """片頭區的結束、片尾區的開始（ticks）；片長不明時片頭區用上限、沒有片尾區。"""
    if not runtime:
        return INTRO_WINDOW, None
    zone = int(runtime * ZONE)
    return min(INTRO_WINDOW, zone), runtime - min(CREDITS_WINDOW, zone)


class IntroLearner:
    def __init__(self, db: Database, config):
        self.db = db
        self.config = config
        self._sessions: Dict[Tuple[str, int], dict] = {}
        self._lock = threading.Lock()
        self.clock: Callable[[], float] = time.monotonic

    @property
    def enabled(self) -> bool:
        return bool(self.config.server.intro_skip)

    # ---------------- 學 ----------------

    def report(self, user_id: str, item, pos: int, stopped: bool) -> None:
        """播放器回報進度時呼叫。"""
        if not self.enabled or item["type"] != "Episode" or pos < 0:
            return
        key = (user_id or "", int(item["id"]))
        now = self.clock()
        runtime = int(item["runtime_ticks"] or 0)
        with self._lock:
            if len(self._sessions) > MAX_SESSIONS:
                self._sessions = {k: s for k, s in self._sessions.items() if now - s["at"] < SESSION_TTL}
            s = self._sessions.get(key)
            if s is None or now - s["at"] > SESSION_TTL:
                self._sessions[key] = {"pos": pos, "at": now}
                if stopped:
                    self._credits(user_id, item, pos, runtime)
                return
            elapsed_ticks = int((now - s["at"]) * TICK)
            delta = pos - s["pos"]
            intro_end, credits_start = windows(runtime)
            # 往前跳：位置前進得比實際時間多很多（3 倍以上再加 15 秒，倍速播放不算）。只看跳的那一下
            if delta >= MIN_JUMP_TICKS and delta > elapsed_ticks * 3 + MIN_JUMP_TICKS:
                if s["pos"] < intro_end and delta <= MAX_INTRO_TICKS:  # 在開頭跳過一段片頭長度的東西
                    self._save(item["id"], user_id, "intro", s["pos"], pos)
                    log.info("學到片頭：%s 第 %s 集 %.0f–%.0f 秒", item["name"], item["index_number"], s["pos"] / TICK, pos / TICK)
                elif credits_start is not None and s["pos"] >= credits_start and (
                    pos >= runtime - MIN_CREDITS_TICKS or delta >= MIN_CREDITS_JUMP
                ):
                    self._save(item["id"], user_id, "credits", s["pos"], runtime)  # 從片尾跳到結尾，或跳過 ED
                    log.info("學到片尾：%s 第 %s 集 %.0f 秒起", item["name"], item["index_number"], s["pos"] / TICK)
            s["pos"], s["at"] = pos, now
            if stopped:
                self._credits(user_id, item, pos, runtime)
                self._sessions.pop(key, None)

    def _credits(self, user_id: str, item, pos: int, runtime: int) -> None:
        """在片尾區停下、但離結尾還有 20 秒以上：多半是片尾切下一集。"""
        credits_start = windows(runtime)[1]
        if credits_start is not None and credits_start <= pos <= runtime - MIN_CREDITS_TICKS:
            self._save(item["id"], user_id, "credits", pos, runtime)
            log.info("學到片尾：%s 第 %s 集 %.0f 秒起", item["name"], item["index_number"], pos / TICK)

    def _save(self, item_id: int, user_id: str, kind: str, start: int, end: int) -> None:
        self.db.execute(
            "INSERT INTO intro_obs(item_id, user_id, kind, start_ticks, end_ticks, at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(item_id, user_id, kind) DO UPDATE SET start_ticks=excluded.start_ticks, "
            "end_ticks=excluded.end_ticks, at=excluded.at",
            (int(item_id), user_id or "", kind, int(start), int(end), int(time.time())),
        )

    # ---------------- 用 ----------------

    def marks_for(self, item) -> dict:
        """這一集的片頭 (start, end) 和片尾 start（ticks）；沒有的鍵不出現。手動設定優先於學到的值。"""
        if not self.enabled or item["type"] != "Episode":
            return {}
        manual = self.manual(item["season_id"])
        out = self.learned_for(item)
        if not manual:
            return out
        if manual["intro_mode"] == "manual":
            out["intro"] = (manual["intro_start"], manual["intro_end"])
        elif manual["intro_mode"] == "none":
            out.pop("intro", None)
        runtime = int(item["runtime_ticks"] or 0)
        if manual["credits_mode"] == "manual" and runtime > manual["credits_tail"]:
            out["credits"] = runtime - manual["credits_tail"]
        elif manual["credits_mode"] != "auto":
            out.pop("credits", None)  # 標成沒有片尾，或不知道片長、算不出從哪裡開始
        return out

    def learned_for(self, item) -> dict:
        """只看學到的：這一集自己的紀錄，沒有就用同一季其他集的中位數。"""
        out: dict = {}
        own = self.db.query("SELECT kind, start_ticks, end_ticks FROM intro_obs WHERE item_id=?", (item["id"],))
        intro = [(r["start_ticks"], r["end_ticks"]) for r in own if r["kind"] == "intro"]
        credits = [r["start_ticks"] for r in own if r["kind"] == "credits"]
        season = self.db.query(
            "SELECT o.kind, o.start_ticks, o.end_ticks, i.runtime_ticks FROM intro_obs o JOIN items i ON i.id=o.item_id "
            "WHERE i.season_id=? AND o.item_id<>?", (item["season_id"], item["id"]),
        ) if item["season_id"] else []
        if not intro:
            intro = [(r["start_ticks"], r["end_ticks"]) for r in season if r["kind"] == "intro"]
        if intro:
            start = int(statistics.median(s for s, _ in intro))
            end = int(statistics.median(e for _, e in intro))
            if end > start:
                out["intro"] = (start, end)
        runtime = int(item["runtime_ticks"] or 0)
        if credits:
            out["credits"] = int(statistics.median(credits))
        elif runtime:
            tails = [r["end_ticks"] - r["start_ticks"] for r in season
                     if r["kind"] == "credits" and r["end_ticks"] > r["start_ticks"]]
            if tails:
                out["credits"] = max(0, runtime - int(statistics.median(tails)))
        return out

    def chapters_for(self, item) -> List[dict]:
        """Emby 的章節標記。"""
        marks = self.marks_for(item)
        out = []
        if "intro" in marks:
            s, e = marks["intro"]
            out += [
                {"StartPositionTicks": s, "Name": "Intro Start", "MarkerType": "IntroStart"},
                {"StartPositionTicks": e, "Name": "Intro End", "MarkerType": "IntroEnd"},
            ]
        if "credits" in marks:
            out.append({"StartPositionTicks": marks["credits"], "Name": "Credits Start", "MarkerType": "CreditsStart"})
        return out

    def skipper_for(self, item, runtime: Optional[int] = None) -> dict:
        """Jellyfin Intro Skipper 外掛的格式（秒）。"""
        marks = self.marks_for(item)
        runtime = runtime or int(item["runtime_ticks"] or 0)

        def seg(start: int, end: int) -> dict:
            return {
                "EpisodeId": str(item["id"]), "Valid": True, "IntroStart": start / TICK, "IntroEnd": end / TICK,
                "ShowSkipPromptAt": start / TICK, "HideSkipPromptAt": min(end, start + 10 * TICK) / TICK,
            }

        empty = {"EpisodeId": str(item["id"]), "Valid": False, "IntroStart": 0, "IntroEnd": 0,
                 "ShowSkipPromptAt": 0, "HideSkipPromptAt": 0}
        intro = seg(*marks["intro"]) if "intro" in marks else empty
        credits = seg(marks["credits"], runtime) if "credits" in marks and runtime else empty
        return {"Introduction": intro, "Credits": credits}

    def segments_for(self, item) -> List[dict]:
        """Jellyfin 10.10 的 MediaSegments。"""
        marks = self.marks_for(item)
        runtime = int(item["runtime_ticks"] or 0)
        out = []
        if "intro" in marks:
            s, e = marks["intro"]
            out.append({"Id": f"{item['id']}-intro", "ItemId": str(item["id"]), "Type": "Intro",
                        "StartTicks": s, "EndTicks": e})
        if "credits" in marks and runtime:
            out.append({"Id": f"{item['id']}-outro", "ItemId": str(item["id"]), "Type": "Outro",
                        "StartTicks": marks["credits"], "EndTicks": runtime})
        return out

    # ---------------- 手動設定 ----------------

    def manual(self, season_id: Optional[int]):
        if not season_id:
            return None
        return self.db.one("SELECT * FROM intro_manual WHERE season_id=?", (int(season_id),))

    def set_manual(self, season_id: int, intro_mode: str = "auto", intro_start: float = 0, intro_end: float = 0,
                   credits_mode: str = "auto", credits_tail: float = 0) -> None:
        """手動設定一季（時間用秒）。片頭、片尾都是 auto 時刪掉設定，改回照學的。值不合理時丟 ValueError。"""
        if intro_mode not in MODES or credits_mode not in MODES:
            raise ValueError("模式只能是 auto、manual 或 none")
        start, end, tail = (int(round(float(v or 0) * TICK)) for v in (intro_start, intro_end, credits_tail))
        if intro_mode == "manual" and not (0 <= start < end <= MAX_MANUAL_TICKS):
            raise ValueError("片頭的開始要早於結束，而且都在 60 分鐘內")
        if credits_mode == "manual" and not (0 < tail <= MAX_MANUAL_TICKS):
            raise ValueError("片尾要填結尾前多久開始，1 秒到 60 分鐘")
        if intro_mode == "auto" and credits_mode == "auto":
            self.db.execute("DELETE FROM intro_manual WHERE season_id=?", (season_id,))
            return
        self.db.execute(
            "INSERT INTO intro_manual(season_id, intro_mode, intro_start, intro_end, credits_mode, credits_tail, at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(season_id) DO UPDATE SET intro_mode=excluded.intro_mode, "
            "intro_start=excluded.intro_start, intro_end=excluded.intro_end, credits_mode=excluded.credits_mode, "
            "credits_tail=excluded.credits_tail, at=excluded.at",
            (season_id, intro_mode, start if intro_mode == "manual" else None, end if intro_mode == "manual" else None,
             credits_mode, tail if credits_mode == "manual" else None, int(time.time())),
        )

    def sibling_seasons(self, season_id: int) -> List[int]:
        """同一部劇的所有季（含自己）。"""
        row = self.db.one("SELECT series_id FROM items WHERE id=? AND type='Season'", (season_id,))
        if not row:
            return []
        return [r["id"] for r in self.db.query("SELECT id FROM items WHERE type='Season' AND series_id=?", (row["series_id"],))]

    def seasons(self, query: str = "", limit: int = 20, offset: int = 0) -> dict:
        """網頁上的清單：沒有搜尋時列有學到紀錄或手動設定的季（最近有動靜的先）；搜尋時列符合的劇的每一季。"""
        where, params = ["se.type='Season'"], []
        obs = "SELECT {} FROM intro_obs o JOIN items e ON e.id=o.item_id WHERE e.season_id=se.id"
        if query.strip():
            sql, more = title_match(query, "s.name", "s.original_title", "s.search_text")
            where.append(sql)
            params += more
            order = "s.sort_name, s.id, se.index_number"
        else:
            # 先從兩張小表挑出有紀錄或手動設定的季，不要對媒體庫裡每一季逐一去查（幾千季時要十幾秒，期間資料庫被佔住）
            where.append("se.id IN (SELECT e.season_id FROM intro_obs o JOIN items e ON e.id=o.item_id "
                         "UNION SELECT season_id FROM intro_manual)")
            order = f"MAX(COALESCE(m.at, 0), COALESCE(({obs.format('MAX(o.at)')}), 0)) DESC, se.id"
        base = (f"FROM items se JOIN items s ON s.id=se.series_id LEFT JOIN intro_manual m ON m.season_id=se.id "
                f"WHERE {' AND '.join(where)}")
        total = self.db.one(f"SELECT COUNT(*) AS c {base}", params)["c"]
        rows = self.db.query(
            f"SELECT se.id AS season_id, se.series_id, s.name AS series, s.year, se.index_number AS season, "
            f"(SELECT COUNT(*) FROM items e WHERE e.season_id=se.id AND e.type='Episode') AS episodes, "
            f"({obs.format('COUNT(DISTINCT o.item_id)')}) AS learned, ({obs.format('MAX(o.at)')}) AS last_at "
            f"{base} ORDER BY {order} LIMIT ? OFFSET ?", (*params, limit, offset),
        )
        return {"items": [self._season_view(r) for r in rows], "total": total}

    def _season_view(self, r) -> dict:
        """一季在網頁上的樣子：現在給播放器的值（手動優先）、學到的值（給編輯時當起點），以及手動設定。"""
        sec = lambda t: round(t / TICK) if t is not None else None  # noqa: E731
        sample = self.db.one(
            "SELECT * FROM items WHERE season_id=? AND type='Episode' ORDER BY index_number LIMIT 1", (r["season_id"],)
        )
        runtime = int(sample["runtime_ticks"] or 0) if sample else 0

        def view(marks: dict) -> dict:
            intro = marks.get("intro")
            credits = marks.get("credits")
            return {"intro": [sec(intro[0]), sec(intro[1])] if intro else None,
                    "credits_tail": sec(runtime - credits) if credits is not None and runtime else None}

        manual = self.manual(r["season_id"])
        return {
            "season_id": r["season_id"], "series_id": r["series_id"], "series": r["series"], "year": r["year"],
            "season": r["season"], "episodes": r["episodes"], "learned": r["learned"], "last_at": r["last_at"],
            "runtime": sec(runtime) if runtime else None,
            **(view(self.marks_for(sample)) if sample else {"intro": None, "credits_tail": None}),
            "auto": view(self.learned_for(sample)) if sample else {"intro": None, "credits_tail": None},
            "manual": {
                "intro_mode": manual["intro_mode"], "intro": [sec(manual["intro_start"]), sec(manual["intro_end"])]
                if manual["intro_mode"] == "manual" else None,
                "credits_mode": manual["credits_mode"], "credits_tail": sec(manual["credits_tail"]),
            } if manual else None,
        }

    # ---------------- 網頁 ----------------

    def status(self) -> dict:
        """管理網頁上的總數：開了沒有、學到幾集、幾季，手動設定了幾季。"""
        count = self.db.scalar
        return {
            "enabled": self.enabled,
            "episodes": count("SELECT COUNT(DISTINCT item_id) FROM intro_obs"),
            "seasons": count("SELECT COUNT(DISTINCT i.season_id) FROM intro_obs o JOIN items i ON i.id=o.item_id"),
            "manual": count("SELECT COUNT(*) FROM intro_manual"),
        }

    def clear(self, season_id: Optional[int] = None) -> int:
        """清掉學到的紀錄（手動設定不動）。"""
        if season_id:
            return self.db.execute(
                "DELETE FROM intro_obs WHERE item_id IN (SELECT id FROM items WHERE season_id=?)", (season_id,)
            ).rowcount
        return self.db.execute("DELETE FROM intro_obs").rowcount

    def prune(self) -> None:
        self.db.execute("DELETE FROM intro_obs WHERE item_id NOT IN (SELECT id FROM items)")
        self.db.execute("DELETE FROM intro_manual WHERE season_id NOT IN (SELECT id FROM items)")
