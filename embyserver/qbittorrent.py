"""qBittorrent：下載中的種子太久沒速度就刪掉，排在後面的接著開始。

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
# 正在下載的狀態：只有這幾種會算沒速度的時間（排隊、暫停、校驗、搬檔案的不算）
DOWNLOADING = frozenset({"downloading", "stalledDL", "metaDL", "forcedDL", "forcedMetaDL"})
# 排在後面等著的：佇列裡的，和停下來的（5.0 起叫 stoppedDL，以前叫 pausedDL）
WAITING = frozenset({"queuedDL", "stoppedDL", "pausedDL"})
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
    downloading: int = 0
    waiting: int = 0
    slow: List[dict] = field(default_factory=list)  # 現在沒速度的（久的在前）：hash、name、progress、quiet（秒）、seeds
    removed: List[dict] = field(default_factory=list)  # 這一輪刪掉的
    started: List[dict] = field(default_factory=list)  # 這一輪接著開始的

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

    def _request(self, method: str, path: str, data: Optional[dict] = None) -> httpx.Response:
        """呼叫 WebUI API；被拒（403 = 還沒登入或登入過期）就登入一次再送。任何失敗都丟 QBittorrentError。"""
        if not self.cfg.url:
            raise QBittorrentError("還沒設定 qBittorrent 網址")
        url = self.cfg.url.rstrip("/") + path
        client = self._http()
        try:
            resp = client.request(method, url, data=data)
            if resp.status_code == 403:
                self._login(client)
                resp = client.request(method, url, data=data)
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

    def torrents(self) -> List[dict]:
        resp = self._request("GET", "/api/v2/torrents/info")
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, list):
            raise QBittorrentError("回應不是 qBittorrent 的種子清單：網址是不是填成了別的服務？")
        return [t for t in data if isinstance(t, dict) and t.get("hash")]

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

    def check(self) -> dict:
        """看一次：記下每個下載中的種子從什麼時候開始沒速度；開了自動刪除時，沒速度太久的刪掉，排在後面的接著開始。"""
        with self._check_lock:
            now = self._clock()
            try:
                items = self.torrents()
            except QBittorrentError as exc:
                self._quiet.clear()  # 連不上的這段時間不知道有沒有速度，連上之後從頭算，免得一連上就刪
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
            self._last_ok = now
            slow = self._track(items, now)
            removed: List[dict] = []
            started: List[dict] = []
            error = ""
            if self.cfg.remove_stalled:
                limit = self.cfg.stalled_minutes * 60
                # 只刪做種數為 0 的：還有人做種就先不刪、只列出來等；計時照算，做種數掉到 0 的下一輪就刪
                stalled = [t for t in slow if now - self._quiet[t["hash"]][0] >= limit
                           and not (self.cfg.no_seeds_only and _seeds(t) > 0)]
                if stalled:
                    try:
                        removed = self._remove(stalled, now)
                        # 強制開始的不佔佇列名額，刪了也不會空出位子
                        freed = [t for t in stalled if not str(t.get("state") or "").startswith("forced")]
                        started = self._start_next(items, {t["hash"] for t in stalled}, len(freed))
                    except QBittorrentError as exc:
                        error = str(exc)
                        log.warning("qBittorrent：%s", exc)
            gone = {r["hash"] for r in removed}
            slow = [t for t in slow if t["hash"] not in gone]
            self.last = CheckResult(
                at=time.time(), error=error,
                downloading=sum(1 for t in items if t.get("state") in DOWNLOADING and _unfinished(t)) - len(removed),
                waiting=sum(1 for t in items if t.get("state") in WAITING and _unfinished(t)),
                slow=[{"hash": t["hash"], "name": t.get("name") or "", "progress": float(t.get("progress") or 0),
                       "quiet": int(now - self._quiet[t["hash"]][0]), "seeds": _seeds(t)} for t in slow[:SLOW_SHOWN]],
                removed=removed, started=[{"hash": t["hash"], "name": t.get("name") or ""} for t in started],
            )
            return self.last

    def _track(self, items: List[dict], now: float) -> List[dict]:
        """更新每個下載中的種子「從什麼時候開始沒速度」，回傳現在沒速度的（久的在前）。

        從那時起平均速度超過 stalled_speed 就算有速度、從現在重新算；不是下載中的（排隊、暫停、下載完）不記，
        之後再開始下載時從頭算，所以排隊很久才輪到的不會一開始就被刪。"""
        limit = max(0.0, float(self.cfg.stalled_speed)) * 1024
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
        self._quiet = quiet
        return sorted(slow, key=lambda t: quiet[t["hash"]][0])

    def _remove(self, stalled: List[dict], now: float) -> List[dict]:
        files = self.cfg.delete_files
        self._request("POST", "/api/v2/torrents/delete",
                      {"hashes": "|".join(t["hash"] for t in stalled), "deleteFiles": "true" if files else "false"})
        rows = []
        for t in stalled:
            minutes = int((now - self._quiet.pop(t["hash"])[0]) // 60)
            rows.append({"hash": t["hash"], "name": t.get("name") or "", "size": int(t.get("size") or 0),
                         "progress": float(t.get("progress") or 0), "category": t.get("category") or "",
                         "minutes": minutes, "files": files, "seeds": _seeds(t), "at": int(time.time())})
            log.info("qBittorrent：「%s」已經 %s 分鐘沒速度（下載了 %.1f%%），刪掉了%s", rows[-1]["name"], minutes,
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
        return waiting

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
                "no_seeds_only": self.cfg.no_seeds_only, "every": CHECK_EVERY, **self.last.as_dict(),
                "history": self.removed()[:20]}

    # ---------------- 定時 ----------------

    def start(self) -> None:
        """每 CHECK_EVERY 秒看一次（沒填網址、沒開自動刪除就不看）；wake() 叫它馬上看。"""

        def loop():
            while not self._stop.is_set():
                if self.enabled and self.cfg.remove_stalled:
                    try:
                        self.check()
                    except Exception:
                        log.exception("檢查 qBittorrent 時出錯")
                self._wake.wait(CHECK_EVERY)
                self._wake.clear()

        self.workers.start(loop)

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
