"""qBittorrent：下載中的種子太久沒速度就刪掉，排在後面的接著開始；不夠幾個在下載就強制開始，開了沒速度的也刪。

詳細說明見 docs/modules.md 的「embyserver/qbittorrent.py」。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

from .config import QBittorrentConfig
from .db import Database
from .http_util import describe
from .workers import Workers

log = logging.getLogger(__name__)

CHECK_EVERY = 300  # 開了自動刪除時，幾秒看一次
TIMEOUT = 15.0
LOGIN_RETRY = 1800  # 登入失敗後隔多久才再試：qBittorrent 連續失敗幾次就封 IP 一小時，不能每 5 分鐘撞一次
RETRY_AFTER = 3600  # 強制開始過、沒刪成（tracker 沒回應、還有人做種）的種子，隔多久才再試它
# 正在下載的狀態：只有這幾種會算沒速度的時間（排隊、暫停、校驗、搬檔案的不算）
DOWNLOADING = frozenset({"downloading", "stalledDL", "metaDL", "forcedDL", "forcedMetaDL"})
# 排在後面等著的：佇列裡的，和停下來的（5.0 起叫 stoppedDL，以前叫 pausedDL）
WAITING = frozenset({"queuedDL", "stoppedDL", "pausedDL"})
TRACKER_WORKING = 2  # torrents/trackers 的 status：0 停用、1 還沒連過、2 正常、3 更新中、4 連不上
REMOVED_KEY = "qb_removed"  # 刪掉的種子（資料庫 meta，JSON，新的在前）
REMOVED_KEEP = 100
SLOW_SHOWN = 30  # 網頁上最多列幾個沒速度的


class QBittorrentError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status  # qBittorrent 回的 HTTP 狀態碼；連不上時是 None


@dataclass
class CheckResult:
    """上一輪看到的。"""

    at: float = 0.0  # 看的時間（牆上時鐘）
    error: str = ""
    downloading: int = 0  # 下載中的（含沒速度的）
    moving: int = 0  # 下載中而且有速度的
    waiting: int = 0  # 排在後面等著的
    forcing: int = 0  # Mi302 強制開始、還在等它有沒有速度的
    slow: List[dict] = field(default_factory=list)  # 現在沒速度的（久的在前）：hash、name、progress、quiet（秒）、seeds、forced
    removed: List[dict] = field(default_factory=list)  # 這一輪刪掉的
    started: List[dict] = field(default_factory=list)  # 這一輪接著開始、強制開始的
    skipped: List[dict] = field(default_factory=list)  # 這一輪強制開始後沒刪、放回佇列的：hash、name、why

    def as_dict(self) -> dict:
        return asdict(self)


def _unfinished(t: dict) -> bool:
    return float(t.get("progress") or 0) < 1


def _seeds(t: dict) -> int:
    """做種數：tracker 回報的（num_complete；沒回報是 -1，當成 0）和實際連上的（num_seeds）取大的。"""
    return max(int(t.get("num_complete") or 0), int(t.get("num_seeds") or 0), 0)


def _queue_order(t: dict) -> tuple:
    """佇列裡的順序：priority 是佇列位置（1 起）；沒開佇列時是 0 或 -1，照加入的先後。"""
    pos = int(t.get("priority") or 0)
    return (pos if pos > 0 else float("inf"), int(t.get("added_on") or 0))


def _brief(t: dict) -> dict:
    return {"hash": t["hash"], "name": t.get("name") or ""}


class QBittorrent:
    def __init__(self, cfg: QBittorrentConfig, db: Optional[Database] = None, transport=None):
        self.cfg = cfg
        self.db = db
        self._transport = transport  # 測試換成假的 qBittorrent
        self._client: Optional[httpx.Client] = None
        self._client_key: Tuple[str, str, str] = ("", "", "")
        self._client_lock = threading.Lock()
        self._login_failed: Tuple[Tuple[str, str, str], float] = (("", "", ""), 0.0)  # (失敗時的網址帳密, 時間)
        # 下載中的種子：hash → (從什麼時候開始沒速度, 那時已經下載了幾 bytes)。只記在記憶體，重新啟動後從頭算
        self._quiet: Dict[str, Tuple[float, int]] = {}
        self._seen: frozenset = frozenset()  # 上一輪就在下載的：這一輪計時剛歸零才真的是「有下載到東西」，第一次看到的不算
        # Mi302 強制開始、還在等它有沒有速度的：hash → (什麼時候開始的, 那時已經下載了幾 bytes)
        self._forced: Dict[str, Tuple[float, int]] = {}
        self._tried: Dict[str, float] = {}  # 強制開始過、沒刪成的：hash → 時間；RETRY_AFTER 以內不再試它
        self._last_ok = 0.0  # 上次讀到種子清單的時間
        self._offline = False  # 上一輪連不上：連回來時記一行
        self._removed: List[dict] = []  # 沒有資料庫時（測試）記在這裡
        self._clock = time.monotonic  # 算間隔用，時間被校正也不受影響；寫進紀錄、給網頁看的另外用 time.time()。測試換成假的
        self._check_lock = threading.Lock()  # 一次只做一輪
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.workers = Workers(self._stop, busy=self._check_lock)
        self.last = CheckResult()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.url)

    # ---------------- HTTP ----------------

    def _http(self) -> httpx.Client:
        """共用一個連線：登入後的 cookie 存在裡面。網址或帳號密碼改了就換一個新的，重新登入。"""
        key = (self.cfg.url, self.cfg.username, self.cfg.password)
        with self._client_lock:
            if self._client is None or key != self._client_key:
                if self._client is not None:
                    self._client.close()
                self._client = httpx.Client(timeout=TIMEOUT, transport=self._transport)
                self._client_key = key
            return self._client

    def _request(self, method: str, path: str, data: Optional[dict] = None, params: Optional[dict] = None) -> httpx.Response:
        """呼叫 WebUI API；被拒（403 = 還沒登入或登入過期）就登入一次再送。任何失敗都丟 QBittorrentError。"""
        if not self.cfg.url:
            raise QBittorrentError("還沒設定 qBittorrent 網址")
        url = self.cfg.url.rstrip("/") + path
        client = self._http()
        try:
            resp = client.request(method, url, data=data, params=params)
            if resp.status_code == 403:
                self._login(client)
                resp = client.request(method, url, data=data, params=params)
        except httpx.InvalidURL as exc:  # 網址填錯（例如埠號不是數字）；不是 httpx.HTTPError
            raise QBittorrentError(f"qBittorrent 網址格式不對：{exc}") from exc
        except httpx.HTTPError as exc:
            raise QBittorrentError(describe(exc)) from exc
        except RuntimeError as exc:  # 設定剛改、連線被換掉，這個請求用到的是關掉的舊連線：下一輪就正常
            raise QBittorrentError(f"qBittorrent 的連線剛重建：{exc}") from exc
        if resp.status_code == 403:
            raise QBittorrentError("qBittorrent 登入後還是拒絕存取", 403)
        if resp.status_code == 401:
            raise QBittorrentError("qBittorrent 回應 401：多半是用主機名稱連、被它的 Host 標頭驗證擋下。"
                                   "改用 IP 連，或在 qBittorrent 的 WebUI 設定把這個主機名稱加進允許的網域", 401)
        if resp.status_code >= 400:
            raise QBittorrentError(f"qBittorrent 回應 HTTP {resp.status_code}：{resp.text[:200]}", resp.status_code)
        return resp

    def _login(self, client: httpx.Client) -> None:
        if not (self.cfg.username or self.cfg.password):
            raise QBittorrentError("qBittorrent 要登入：請填 WebUI 的帳號密碼，或在 qBittorrent 設定「本機略過驗證」", 403)
        key, at = self._login_failed
        if key == self._client_key and self._clock() - at < LOGIN_RETRY:
            # 同一組帳密剛失敗過就不再撞：qBittorrent 連續失敗幾次就封 IP，MoviePilot 從同一台連的話會一起被擋
            raise QBittorrentError(f"qBittorrent 帳號或密碼不對；{LOGIN_RETRY // 60} 分鐘內不再試，免得連續失敗被它封鎖 IP"
                                   "（改了帳號密碼會馬上再試）", 403)
        resp = client.post(self.cfg.url.rstrip("/") + "/api/v2/auth/login",
                           data={"username": self.cfg.username, "password": self.cfg.password})
        if resp.status_code == 403:
            raise QBittorrentError("qBittorrent 因為登入失敗太多次，暫時封鎖了這台機器的 IP；過一陣子再試", 403)
        if resp.status_code not in (200, 204) or resp.text.strip() == "Fails.":
            self._login_failed = (self._client_key, self._clock())
            raise QBittorrentError(f"qBittorrent 帳號或密碼不對（HTTP {resp.status_code}）", resp.status_code)

    def _json_list(self, path: str, params: Optional[dict] = None, what: str = "種子清單") -> List[dict]:
        resp = self._request("GET", path, params=params)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, list):
            raise QBittorrentError(f"回應不是 qBittorrent 的{what}：網址是不是填成了別的服務？")
        return [t for t in data if isinstance(t, dict)]

    def torrents(self) -> List[dict]:
        return [t for t in self._json_list("/api/v2/torrents/info") if t.get("hash")]

    def tracker_working(self, h: str) -> bool:
        """這個種子有沒有 tracker 正常回應（DHT、PeX 這些 ** 開頭的不算）。"""
        rows = self._json_list("/api/v2/torrents/trackers", {"hash": h}, "tracker 清單")
        return any(int(r.get("status") or 0) == TRACKER_WORKING for r in rows
                   if not str(r.get("url") or "").startswith("**"))

    def close(self) -> None:
        with self._client_lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    # ---------------- 測試連線 ----------------

    def test(self) -> dict:
        try:
            items = self.torrents()
            version = self._request("GET", "/api/v2/app/version").text.strip()
        except QBittorrentError as exc:
            return {"ok": False, "message": str(exc)}
        down = sum(1 for t in items if t.get("state") in DOWNLOADING and _unfinished(t))
        waiting = sum(1 for t in items if t.get("state") in WAITING and _unfinished(t))
        return {"ok": True, "message": f"連線成功：qBittorrent {version}，共 {len(items)} 個種子，"
                                       f"下載中 {down} 個，排在後面等著的 {waiting} 個"}

    # ---------------- 沒速度的種子 ----------------

    def check(self) -> CheckResult:
        """看一次：記下每個下載中的種子從什麼時候開始沒速度。開了自動刪除時：沒速度太久的刪掉，排在後面的接著開始；
        強制開始的過了 force_seconds 還沒速度就刪；有速度的不夠 keep_active 個就再強制開始幾個。"""
        with self._check_lock:
            now = self._clock()
            try:
                items = self.torrents()
            except QBittorrentError as exc:
                self._quiet.clear()  # 連不上的這段時間不知道有沒有速度，連上之後從頭算，免得一連上就刪
                self._forced.clear()
                if str(exc) != self.last.error:
                    log.warning("qBittorrent：%s", exc)
                self._offline = True
                self.last = CheckResult(at=time.time(), error=str(exc), downloading=self.last.downloading,
                                        waiting=self.last.waiting, slow=self.last.slow)
                return self.last
            if self._offline:
                log.info("qBittorrent 連上了")
                self._offline = False
            if now - self._last_ok > CHECK_EVERY * 3:
                self._quiet.clear()  # 很久沒看了（剛打開、手動才看一次、電腦睡過）：中間有沒有速度不知道，從頭算
                self._forced.clear()
            self._last_ok = now
            slow = self._track(items, now)
            removed: List[dict] = []
            started: List[dict] = []
            skipped: List[dict] = []
            error = ""
            if self.cfg.remove_stalled:
                try:
                    removed, started, skipped = self._enforce(items, slow, now)
                except QBittorrentError as exc:
                    error = str(exc)
                    log.warning("qBittorrent：%s", exc)
            else:
                self._forced.clear()
            gone = {r["hash"] for r in removed}
            slow = [t for t in slow if t["hash"] not in gone]
            downloading = [t for t in items if t["hash"] not in gone and t.get("state") in DOWNLOADING and _unfinished(t)]
            self.last = CheckResult(
                at=time.time(), error=error,
                downloading=len(downloading), moving=sum(1 for t in downloading if self._moving(t, now)),
                waiting=sum(1 for t in items if t["hash"] not in gone and t.get("state") in WAITING and _unfinished(t)),
                forcing=len(self._forced), slow=self._slow_entries(slow, downloading, now),
                removed=removed, started=started, skipped=skipped,
            )
            return self.last

    def _track(self, items: List[dict], now: float) -> List[dict]:
        """更新每個下載中的種子「從什麼時候開始沒速度」，回傳現在沒速度的（久的在前）。

        從那時起平均速度超過 stalled_speed 就算有速度、從現在重新算；不是下載中的（排隊、暫停、下載完）不記，
        之後再開始下載時從頭算，所以排隊很久才輪到的不會一開始就被刪。"""
        limit = self._limit()
        quiet: Dict[str, Tuple[float, int]] = {}
        slow = []
        for t in items:
            if t.get("state") not in DOWNLOADING or not _unfinished(t):
                continue
            h, got = t["hash"], int(t.get("downloaded") or 0)
            since, base = self._quiet.get(h, (now, got))
            if got < base or got - base > limit * (now - since):  # 有速度，或重新校驗後數字變小了
                since, base = now, got
            quiet[h] = (since, base)
            if now > since:
                slow.append(t)
        self._seen = frozenset(self._quiet)
        self._quiet = quiet
        return sorted(slow, key=lambda t: quiet[t["hash"]][0])

    def _slow_entries(self, slow: List[dict], downloading: List[dict], now: float) -> List[dict]:
        """網頁上列的沒速度的種子：一般的從開始沒速度算；Mi302 強制開始、還在等的從強制開始算（剛開始的這一輪也列）。"""
        listed = {t["hash"] for t in slow}
        rows = list(slow) + [t for t in downloading if t["hash"] in self._forced and t["hash"] not in listed]

        def entry(t: dict) -> dict:
            h = t["hash"]
            since = self._forced[h][0] if h in self._forced else self._quiet[h][0]
            return {"hash": h, "name": t.get("name") or "", "progress": float(t.get("progress") or 0),
                    "quiet": int(now - since), "seeds": _seeds(t), "forced": h in self._forced}

        return [entry(t) for t in rows[:SLOW_SHOWN]]

    def _limit(self) -> float:
        """算沒速度的門檻，bytes／秒。"""
        return max(0.0, float(self.cfg.stalled_speed)) * 1024

    def _moving(self, t: dict, now: float) -> bool:
        """現在有速度：這一刻的速度超過門檻，或上一輪看到現在有下載到東西（_track 剛把計時歸零；第一次看到的不算）。"""
        h = t["hash"]
        return float(t.get("dlspeed") or 0) > self._limit() or (h in self._seen and self._quiet.get(h, (0.0, 0))[0] == now)

    def _enforce(self, items: List[dict], slow: List[dict], now: float) -> Tuple[List[dict], List[dict], List[dict]]:
        """自動刪除開著時的一輪：刪沒速度太久的、判斷強制開始的、不夠就再強制開始。回傳 (刪掉的, 開始的, 放回佇列的)。"""
        removed: List[dict] = []
        started: List[dict] = []
        skipped: List[dict] = []
        limit = self.cfg.stalled_minutes * 60
        # 只刪做種數為 0 的：還有人做種就先不刪、只列出來等；計時照算，做種數掉到 0 的下一輪就刪
        stalled = [t for t in slow if t["hash"] not in self._forced and now - self._quiet[t["hash"]][0] >= limit
                   and not (self.cfg.no_seeds_only and _seeds(t) > 0)]
        if stalled:
            removed += self._remove(stalled, now, "stalled")
            # 強制開始的不佔佇列名額，刪了也不會空出位子
            freed = [t for t in stalled if not str(t.get("state") or "").startswith("forced")]
            started += self._start_next(items, {t["hash"] for t in stalled}, len(freed))
        gone = {r["hash"] for r in removed}
        items = [t for t in items if t["hash"] not in gone]
        if self.cfg.keep_active > 0:
            bad, since, back = self._judge_forced(items, now)
            if bad:
                removed += self._remove(bad, now, "forced", since)
                gone = {t["hash"] for t in bad}
                items = [t for t in items if t["hash"] not in gone]
            skipped += back
            started += self._force_more(items, now)
        else:
            self._forced.clear()  # 關掉了：強制開始過、還在等的就照一般的規則看
        return removed, started, skipped

    def _judge_forced(self, items: List[dict], now: float) -> Tuple[List[dict], Dict[str, float], List[dict]]:
        """Mi302 強制開始的：有速度了就不管它；過了 force_seconds 還沒速度，tracker 有正常回應的算種子有問題（要刪），
        tracker 沒回應的是 tracker 或網路的問題、還有人做種的（no_seeds_only）先不刪：取消強制、放回佇列，一陣子不再試。
        回傳 (要刪的, 它們什麼時候強制開始的, 放回佇列的)。"""
        by_hash = {t["hash"]: t for t in items}
        bad: List[dict] = []
        since: Dict[str, float] = {}
        back: List[dict] = []
        for h, (at, base) in list(self._forced.items()):
            t = by_hash.get(h)
            if t is None or t.get("state") not in DOWNLOADING or not _unfinished(t):
                del self._forced[h]  # 被刪了、下載完了，或被人改回去了
                continue
            if int(t.get("downloaded") or 0) > base or float(t.get("dlspeed") or 0) > 0:
                del self._forced[h]  # 有速度了，之後照一般的規則看；這一輪算它有速度（_track 可能才第一次看到它）
                self._quiet[h] = (now, int(t.get("downloaded") or 0))
                self._seen = self._seen | {h}
                continue
            if now - at < self.cfg.force_seconds:
                continue  # 還在等
            if self.cfg.no_seeds_only and _seeds(t) > 0:
                why = f"還有 {_seeds(t)} 人做種"
            elif not self.tracker_working(h):  # 問不到（丟 QBittorrentError）就還留在等的裡面，下一輪再判
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
            self._request("POST", "/api/v2/torrents/setForceStart", {"hashes": "|".join(t["hash"] for t in back), "value": "false"})
            log.info("qBittorrent：強制開始後 %s 秒沒速度但先不刪、放回佇列：%s", self.cfg.force_seconds,
                     "、".join(f"「{t['name'] or t['hash']}」（{t['why']}）" for t in back))
        return bad, since, back

    def _force_more(self, items: List[dict], now: float) -> List[dict]:
        """有速度的（加上剛強制開始、還在等的）不夠 keep_active 個，就照佇列順序再強制開始幾個；排隊的用完就不開。"""
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
        self._request("POST", "/api/v2/torrents/setForceStart", {"hashes": "|".join(t["hash"] for t in waiting), "value": "true"})
        for t in waiting:
            self._forced[t["hash"]] = (now, int(t.get("downloaded") or 0))
        log.info("qBittorrent：有速度的只有 %s 個，強制開始 %s", active,
                 "、".join(f"「{t.get('name') or t['hash']}」" for t in waiting))
        return [_brief(t) for t in waiting]

    def _remove(self, torrents: List[dict], now: float, rule: str, since: Optional[Dict[str, float]] = None) -> List[dict]:
        """刪掉這些種子，記下來。rule：stalled＝沒速度太久（從開始沒速度算），forced＝強制開始後沒速度（since：什麼時候開始的）。"""
        files = self.cfg.delete_files
        self._request("POST", "/api/v2/torrents/delete",
                      {"hashes": "|".join(t["hash"] for t in torrents), "deleteFiles": "true" if files else "false"})
        rows = []
        for t in torrents:
            quiet_since = self._quiet.pop(t["hash"], (now, 0))[0]
            seconds = int(now - (since or {}).get(t["hash"], quiet_since))
            rows.append({"hash": t["hash"], "name": t.get("name") or "", "size": int(t.get("size") or 0),
                         "progress": float(t.get("progress") or 0), "category": t.get("category") or "",
                         "minutes": seconds // 60, "seconds": seconds, "rule": rule, "files": files, "seeds": _seeds(t),
                         "at": int(time.time())})
            how = f"強制開始後 {seconds} 秒" if rule == "forced" else f"已經 {seconds // 60} 分鐘"
            log.info("qBittorrent：「%s」%s沒速度（下載了 %.1f%%），刪掉了%s", rows[-1]["name"], how,
                     rows[-1]["progress"] * 100, "，連同下載到一半的檔案" if files else "，檔案留著")
        self._record(rows)
        return rows

    def _start_next(self, items: List[dict], gone: set, count: int) -> List[dict]:
        """刪掉幾個就接著開始排在後面的幾個。佇列裡的 qBittorrent 空出名額自己會開始；停下來的要叫它開始。"""
        waiting = sorted((t for t in items if t["hash"] not in gone and t.get("state") in WAITING and _unfinished(t)),
                         key=_queue_order)[:count]
        stopped = [t["hash"] for t in waiting if t.get("state") != "queuedDL"]
        if stopped:
            data = {"hashes": "|".join(stopped)}
            try:
                self._request("POST", "/api/v2/torrents/start", data)
            except QBittorrentError as exc:
                if exc.status != 404:
                    raise
                self._request("POST", "/api/v2/torrents/resume", data)  # 5.0 以前叫 resume
        if waiting:
            log.info("qBittorrent：接著開始 %s", "、".join(f"「{t.get('name') or t['hash']}」" for t in waiting))
        return [_brief(t) for t in waiting]

    # ---------------- 刪除紀錄 ----------------

    def removed(self) -> List[dict]:
        if self.db is None:
            return list(self._removed)
        try:
            rows = json.loads(self.db.get_meta(REMOVED_KEY) or "[]")
        except ValueError:
            return []
        return rows if isinstance(rows, list) else []

    def _record(self, rows: List[dict]) -> None:
        rows = (rows + self.removed())[:REMOVED_KEEP]
        if self.db is None:
            self._removed = rows
            return
        try:
            self.db.set_meta(REMOVED_KEY, json.dumps(rows, ensure_ascii=False))
        except sqlite3.Error as exc:  # 種子已經刪了，紀錄寫不進去不能讓這一輪當成失敗
            log.warning("qBittorrent 的刪除紀錄沒寫進去（%s 個）：%s", len(rows), exc)

    def status(self) -> dict:
        return {"enabled": self.enabled, "active": self.cfg.remove_stalled, "minutes": self.cfg.stalled_minutes,
                "no_seeds_only": self.cfg.no_seeds_only, "keep_active": self.cfg.keep_active,
                "force_seconds": self.cfg.force_seconds, "every": CHECK_EVERY, **self.last.as_dict(),
                "history": self.removed()[:20]}

    # ---------------- 定時 ----------------

    def start(self) -> None:
        """每 CHECK_EVERY 秒看一次（沒填網址、沒開自動刪除就不看）；剛強制開始了種子就過 force_seconds 回來看。
        wake() 叫它馬上看。"""

        def loop():
            while not self._stop.is_set():
                if self.enabled and self.cfg.remove_stalled:
                    try:
                        self.check()
                    except Exception:
                        log.exception("檢查 qBittorrent 時出錯")
                self._wake.wait(self.cfg.force_seconds + 2 if self._forced else CHECK_EVERY)
                self._wake.clear()

        self.workers.start(loop)

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
