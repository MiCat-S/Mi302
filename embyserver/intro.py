"""片頭片尾：從播放行為學出來，給播放器「跳過片頭」「跳過片尾」用。

不碰 115、不讀影片：Mi302 本來就會收到播放器的進度回報（每幾秒一次的位置），從裡面看得出
「在開頭跳過了一段」和「片尾停下來、切下一集」。做法參考 Emby 神醫助手的「片頭探測 ‐ 播放行為」。

- 片頭：開頭 10 分鐘內（短的集是前 25%），位置往前跳了 15 秒到 3 分鐘、而且跳得比實際經過的時間多很多
  （不是倍速播放），就記下「從哪跳到哪」。只看跳的那一下，之後怎麼播不管。
  同一季的其他集沒有自己的紀錄時，套用這一季所有紀錄的中位數。
- 片尾：最後 5 分鐘（短的集是最後 25%）裡停下（切下一集、關掉）但沒播完，或往前跳了 60 秒以上、跳到結尾，
  記下位置；同一季套用「距離結尾多久」的中位數。
- 每個使用者對每一集只留最後一次紀錄，多人多集時取中位數，偶爾亂跳不會蓋掉。

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
        """這一集的片頭 (start, end) 和片尾 start（ticks）；沒有的鍵不出現。"""
        if not self.enabled or item["type"] != "Episode":
            return {}
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
            tails = [r["end_ticks"] - r["start_ticks"] for r in season if r["kind"] == "credits" and r["end_ticks"] > r["start_ticks"]]
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

        empty = {"EpisodeId": str(item["id"]), "Valid": False, "IntroStart": 0, "IntroEnd": 0, "ShowSkipPromptAt": 0, "HideSkipPromptAt": 0}
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
            out.append({"Id": f"{item['id']}-intro", "ItemId": str(item["id"]), "Type": "Intro", "StartTicks": s, "EndTicks": e})
        if "credits" in marks and runtime:
            out.append({"Id": f"{item['id']}-outro", "ItemId": str(item["id"]), "Type": "Outro", "StartTicks": marks["credits"], "EndTicks": runtime})
        return out

    # ---------------- 網頁 ----------------

    def status(self, limit: int = 20) -> dict:
        one = lambda sql: self.db.one(sql)["c"]  # noqa: E731
        rows = self.db.query(
            "SELECT s.name AS series, e.parent_index_number AS season, COUNT(DISTINCT o.item_id) AS episodes, "
            "MIN(o.at) AS first_at, MAX(o.at) AS last_at, "
            "SUM(o.kind='intro') AS intros, SUM(o.kind='credits') AS credits, e.season_id AS season_id "
            "FROM intro_obs o JOIN items e ON e.id=o.item_id LEFT JOIN items s ON s.id=e.series_id "
            "GROUP BY e.season_id ORDER BY last_at DESC LIMIT ?", (limit,),
        )
        seasons = []
        for r in rows:
            sample = self.db.one("SELECT * FROM items WHERE season_id=? AND type='Episode' ORDER BY index_number LIMIT 1", (r["season_id"],))
            marks = self.marks_for(sample) if sample else {}
            seasons.append({
                "series": r["series"], "season": r["season"], "episodes": r["episodes"], "last_at": r["last_at"],
                "intro": [round(marks["intro"][0] / TICK), round(marks["intro"][1] / TICK)] if "intro" in marks else None,
                "credits_tail": round((int(sample["runtime_ticks"]) - marks["credits"]) / TICK)
                if sample and "credits" in marks and sample["runtime_ticks"] else None,
            })
        return {
            "enabled": self.enabled,
            "episodes": one("SELECT COUNT(DISTINCT item_id) AS c FROM intro_obs"),
            "seasons": one("SELECT COUNT(DISTINCT i.season_id) AS c FROM intro_obs o JOIN items i ON i.id=o.item_id"),
            "recent": seasons,
        }

    def clear(self, season_id: Optional[int] = None) -> int:
        if season_id:
            return self.db.execute(
                "DELETE FROM intro_obs WHERE item_id IN (SELECT id FROM items WHERE season_id=?)", (season_id,)
            ).rowcount
        return self.db.execute("DELETE FROM intro_obs").rowcount

    def prune(self) -> None:
        self.db.execute("DELETE FROM intro_obs WHERE item_id NOT IN (SELECT id FROM items)")
