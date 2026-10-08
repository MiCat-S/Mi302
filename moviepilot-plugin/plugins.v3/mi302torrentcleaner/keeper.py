"""判斷沒速度的種子、強制開始、刪除：純邏輯，不碰 MoviePilot，由 __init__.py 接上它的下載器。

和 Mi302 本體 embyserver/qbittorrent.py 是同一套規則（那邊直接連 qBittorrent 的 WebUI）；改規則時兩邊要一起改。
client 要有：torrents() → [dict]（qBittorrent torrents/info 的欄位）、delete(hashes, files)、start(hashes)、
set_force(hashes, on)、tracker_working(hash) → bool。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

CHECK_EVERY = 300  # 幾秒看一次；隔太久沒看（三倍）就從頭算
RETRY_AFTER = 3600  # 強制開始過、沒刪成（tracker 沒回應、還有人做種）的種子，隔多久才再試它
DOWNLOADING = frozenset({"downloading", "stalledDL", "metaDL", "forcedDL", "forcedMetaDL"})  # 正在下載的狀態
WAITING = frozenset({"queuedDL", "stoppedDL", "pausedDL"})  # 排在後面等著的（5.0 起叫 stoppedDL，以前叫 pausedDL）
SLOW_SHOWN = 30


@dataclass
class KeeperConfig:
    stalled_minutes: int = 60  # 連續幾分鐘沒速度算太久
    stalled_speed: float = 0  # 平均速度不超過幾 KB/s 算沒速度；0 = 完全沒下載到東西才算
    delete_files: bool = True  # 連同下載到一半的檔案一起刪
    no_seeds_only: bool = False  # 只刪做種數為 0 的
    keep_active: int = 0  # 隨時要有幾個種子有速度在下載；0 = 不管
    force_seconds: int = 30  # 強制開始後幾秒內沒速度就刪
    tags: Tuple[str, ...] = ()  # 只看帶這些標籤的種子；空的 = 全部


@dataclass
class CheckResult:
    at: float = 0.0
    error: str = ""
    downloading: int = 0
    moving: int = 0
    waiting: int = 0
    forcing: int = 0
    slow: List[dict] = field(default_factory=list)
    removed: List[dict] = field(default_factory=list)
    started: List[dict] = field(default_factory=list)
    skipped: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _unfinished(t: dict) -> bool:
    return float(t.get("progress") or 0) < 1


def _seeds(t: dict) -> int:
    """做種數：tracker 回報的（num_complete；沒回報是 -1，當成 0）和實際連上的（num_seeds）取大的。"""
    return max(int(t.get("num_complete") or 0), int(t.get("num_seeds") or 0), 0)


def _queue_order(t: dict) -> tuple:
    pos = int(t.get("priority") or 0)
    return (pos if pos > 0 else float("inf"), int(t.get("added_on") or 0))


def _brief(t: dict) -> dict:
    return {"hash": t["hash"], "name": t.get("name") or ""}


class Keeper:
    """一個下載器的狀態：每個下載中的種子從什麼時候開始沒速度、強制開始了誰、試過誰。"""

    def __init__(self, cfg: KeeperConfig, clock=time.monotonic):
        self.cfg = cfg
        self._clock = clock
        self._quiet: Dict[str, Tuple[float, int]] = {}  # hash → (從什麼時候開始沒速度, 那時已經下載了幾 bytes)
        self._seen: frozenset = frozenset()  # 上一輪就在下載的
        self._forced: Dict[str, Tuple[float, int]] = {}  # 強制開始、還在等的：hash → (什麼時候開始的, 那時的 bytes)
        self._tried: Dict[str, float] = {}  # 強制開始過、沒刪成的：hash → 時間
        self._last_ok = 0.0

    @property
    def forcing(self) -> bool:
        return bool(self._forced)

    def check(self, client) -> CheckResult:
        now = self._clock()
        try:
            items = self._filter(client.torrents())
        except Exception as exc:  # 連不上、下載器沒連線：這段時間不知道有沒有速度，連上之後從頭算
            self._quiet.clear()
            self._forced.clear()
            return CheckResult(at=time.time(), error=f"{type(exc).__name__}: {exc}")
        if now - self._last_ok > CHECK_EVERY * 3:
            self._quiet.clear()
            self._forced.clear()
        self._last_ok = now
        slow = self._track(items, now)
        error = ""
        removed: List[dict] = []
        started: List[dict] = []
        skipped: List[dict] = []
        try:
            removed, started, skipped = self._enforce(client, items, slow, now)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        gone = {r["hash"] for r in removed}
        slow = [t for t in slow if t["hash"] not in gone]
        downloading = [t for t in items if t["hash"] not in gone and t.get("state") in DOWNLOADING and _unfinished(t)]
        return CheckResult(
            at=time.time(), error=error, downloading=len(downloading),
            moving=sum(1 for t in downloading if self._moving(t, now)),
            waiting=sum(1 for t in items if t["hash"] not in gone and t.get("state") in WAITING and _unfinished(t)),
            forcing=len(self._forced), slow=self._slow_entries(slow, downloading, now),
            removed=removed, started=started, skipped=skipped,
        )

    def _filter(self, items: List[dict]) -> List[dict]:
        items = [t for t in items if t.get("hash")]
        if not self.cfg.tags:
            return items
        want = set(self.cfg.tags)
        return [t for t in items if want <= {x.strip() for x in str(t.get("tags") or "").split(",") if x.strip()}]

    def _limit(self) -> float:
        return max(0.0, float(self.cfg.stalled_speed)) * 1024

    def _track(self, items: List[dict], now: float) -> List[dict]:
        limit = self._limit()
        quiet: Dict[str, Tuple[float, int]] = {}
        slow = []
        for t in items:
            if t.get("state") not in DOWNLOADING or not _unfinished(t):
                continue
            h, got = t["hash"], int(t.get("downloaded") or 0)
            since, base = self._quiet.get(h, (now, got))
            if got < base or got - base > limit * (now - since):
                since, base = now, got
            quiet[h] = (since, base)
            if now > since:
                slow.append(t)
        self._seen = frozenset(self._quiet)
        self._quiet = quiet
        return sorted(slow, key=lambda t: quiet[t["hash"]][0])

    def _moving(self, t: dict, now: float) -> bool:
        h = t["hash"]
        return float(t.get("dlspeed") or 0) > self._limit() or (h in self._seen and self._quiet.get(h, (0.0, 0))[0] == now)

    def _slow_entries(self, slow: List[dict], downloading: List[dict], now: float) -> List[dict]:
        listed = {t["hash"] for t in slow}
        rows = list(slow) + [t for t in downloading if t["hash"] in self._forced and t["hash"] not in listed]

        def entry(t: dict) -> dict:
            h = t["hash"]
            since = self._forced[h][0] if h in self._forced else self._quiet[h][0]
            return {"hash": h, "name": t.get("name") or "", "progress": float(t.get("progress") or 0),
                    "quiet": int(now - since), "seeds": _seeds(t), "forced": h in self._forced}

        return [entry(t) for t in rows[:SLOW_SHOWN]]

    def _enforce(self, client, items, slow, now) -> Tuple[List[dict], List[dict], List[dict]]:
        removed: List[dict] = []
        started: List[dict] = []
        skipped: List[dict] = []
        limit = self.cfg.stalled_minutes * 60
        stalled = [t for t in slow if t["hash"] not in self._forced and now - self._quiet[t["hash"]][0] >= limit
                   and not (self.cfg.no_seeds_only and _seeds(t) > 0)]
        if stalled:
            removed += self._remove(client, stalled, now, "stalled")
            freed = [t for t in stalled if not str(t.get("state") or "").startswith("forced")]
            started += self._start_next(client, items, {t["hash"] for t in stalled}, len(freed))
        gone = {r["hash"] for r in removed}
        items = [t for t in items if t["hash"] not in gone]
        if self.cfg.keep_active > 0:
            bad, since, back = self._judge_forced(client, items, now)
            if bad:
                removed += self._remove(client, bad, now, "forced", since)
                gone = {t["hash"] for t in bad}
                items = [t for t in items if t["hash"] not in gone]
            skipped += back
            started += self._force_more(client, items, now)
        else:
            self._forced.clear()
        return removed, started, skipped

    def _judge_forced(self, client, items, now) -> Tuple[List[dict], Dict[str, float], List[dict]]:
        by_hash = {t["hash"]: t for t in items}
        bad: List[dict] = []
        since: Dict[str, float] = {}
        back: List[dict] = []
        for h, (at, base) in list(self._forced.items()):
            t = by_hash.get(h)
            if t is None or t.get("state") not in DOWNLOADING or not _unfinished(t):
                del self._forced[h]
                continue
            if int(t.get("downloaded") or 0) > base or float(t.get("dlspeed") or 0) > 0:
                del self._forced[h]
                self._quiet[h] = (now, int(t.get("downloaded") or 0))
                self._seen = self._seen | {h}
                continue
            if now - at < self.cfg.force_seconds:
                continue
            if self.cfg.no_seeds_only and _seeds(t) > 0:
                why = f"還有 {_seeds(t)} 人做種"
            elif not client.tracker_working(h):
                why = "tracker 沒回應"
            else:
                why = ""
            del self._forced[h]
            if not why:
                bad.append(t)
                since[h] = at
                continue
            back.append({**_brief(t), "why": why})
            self._tried[h] = now
        if back:
            client.set_force([t["hash"] for t in back], False)
        return bad, since, back

    def _force_more(self, client, items, now) -> List[dict]:
        active = sum(1 for t in items if t.get("state") in DOWNLOADING and _unfinished(t)
                     and (self._moving(t, now) or t["hash"] in self._forced))
        need = self.cfg.keep_active - active
        if need <= 0:
            return []
        self._tried = {h: at for h, at in self._tried.items() if now - at < RETRY_AFTER}
        waiting = sorted((t for t in items if t.get("state") in WAITING and _unfinished(t) and t["hash"] not in self._tried),
                         key=_queue_order)[:need]
        if not waiting:
            return []
        client.set_force([t["hash"] for t in waiting], True)
        for t in waiting:
            self._forced[t["hash"]] = (now, int(t.get("downloaded") or 0))
        return [_brief(t) for t in waiting]

    def _remove(self, client, torrents, now, rule, since: Optional[Dict[str, float]] = None) -> List[dict]:
        files = self.cfg.delete_files
        client.delete([t["hash"] for t in torrents], files)
        rows = []
        for t in torrents:
            quiet_since = self._quiet.pop(t["hash"], (now, 0))[0]
            seconds = int(now - (since or {}).get(t["hash"], quiet_since))
            rows.append({"hash": t["hash"], "name": t.get("name") or "", "size": int(t.get("size") or 0),
                         "progress": float(t.get("progress") or 0), "category": t.get("category") or "",
                         "minutes": seconds // 60, "seconds": seconds, "rule": rule, "files": files, "seeds": _seeds(t),
                         "at": int(time.time())})
        return rows

    def _start_next(self, client, items, gone: set, count: int) -> List[dict]:
        waiting = sorted((t for t in items if t["hash"] not in gone and t.get("state") in WAITING and _unfinished(t)),
                         key=_queue_order)[:count]
        stopped = [t["hash"] for t in waiting if t.get("state") != "queuedDL"]
        if stopped:
            client.start(stopped)
        return [_brief(t) for t in waiting]
