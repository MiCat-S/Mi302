"""刮削交給 MoviePilot：把需要刮削的 strm 路徑送到 MoviePilot 的刮削 API。

詳細說明見 docs/modules.md 的「embyserver/moviepilot.py」。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlsplit

import httpx

from .config import Config, MoviePilotConfig
from .db import Database
from .filetypes import IMAGE_EXTS, LIBRARY_VIDEO_EXTS as VIDEO_EXTS
from .scanner import series_folder
from .textutil import cjk_count, pinyin_full, simplified
from .http_util import describe
from .workers import Workers

log = logging.getLogger(__name__)

SCRAPE_API = "/api/v1/media/scrape/local"
LOGIN_API = "/api/v1/login/access-token"
SUBSCRIBE_API = "/api/v1/subscribe/"
SUBSCRIBE_HISTORY_API = "/api/v1/subscribe/history/"
SUBSCRIBE_SEARCH_API = "/api/v1/subscribe/search/{sid}"
TMDB_EPISODES_API = "/api/v1/tmdb/{tmdbid}/{season}"
TMDB_SEASONS_API = "/api/v1/tmdb/seasons/{tmdbid}"  # 這部劇有哪幾季（確認媒體庫的季在 TMDB 上存不存在）
# 手動整理（在 115 上改名、搬家、刮削）和推薦集數定位模板
TRANSFER_API = "/api/v1/transfer/manual"
EPISODE_FORMAT_API = "/api/v1/transfer/episode-format/recommend"
TRANSFER_NAME_API = "/api/v1/transfer/name"  # 整理後會叫什麼
TRANSFER_TARGET_API = "/api/v1/transfer/manual/target-path"  # 它自己照目錄設定會整理到哪裡
TRANSFER_HISTORY_API = "/api/v1/transfer/manual/history"  # 有沒有成功整理過的紀錄
DIRECTORIES_API = "/api/v1/storage/directories"  # 目錄設定（V3）
DIRECTORIES_V2_API = "/api/v1/system/setting/Directories"  # 目錄設定（V2）
SYSTEM_ENV_API = "/api/v1/system/env"  # 系統設定，裡面有版本號（要管理員帳號）
PLUGIN_API = "/api/v1/plugin/Mi302Organizer"  # Mi302 整理助手外掛（倉庫的 moviepilot-plugin/）
# MoviePilot 3000 埠是前端，API 轉給 3001 的後端；後端忙到沒回應健康檢查時，前端會自己停掉，之後一直連不上
FRONTEND_HINT = "。3000 是 MoviePilot 的前端，後端忙的時候它會自己停掉；可以把網址改成後端的 3001 埠（例如 http://127.0.0.1:3001），不經過前端"
# 手動整理的預覽模式從 v2.11.1-1 開始；更舊的版本不認 preview，「預覽」會變成真的整理
PREVIEW_MIN_VERSION = (2, 11, 1, 1)
MAX_CONCURRENCY = 8  # 同時送幾項刮削的上限
# 送過卻沒有劇照的集（TMDB 沒有這集的圖），這段時間內手動刮削不再重送；過了這段時間的紀錄順手清掉
NO_IMAGE_RETRY_SECONDS = 30 * 86400
FILL_EXCLUDED_KEY = "fill_excluded"  # 補全缺集時跳過的劇（資料庫 meta，JSON：tmdbid → 劇名）
# 這麼久以內查過 TMDB 的季直接用記下的，不再問一次：檢查完接著補全、同一天重跑，不必再問上千次
TMDB_FRESH_SECONDS = 6 * 3600


def parse_version(text: str) -> Optional[Tuple[int, int, int, int]]:
    """「v3.0.9」「v2.11.1-1」→ (3, 0, 9, 0)、(2, 11, 1, 1)；看不懂回傳 None。"""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(\d+))?", str(text or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0)) if m else None


class MoviePilotError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, kind: str = ""):
        super().__init__(message)
        self.status = status  # MoviePilot 回的 HTTP 狀態碼；不是 HTTP 錯誤時是 None
        # 連線出了什麼事：offline＝連不上（請求沒送到，它什麼都沒做）；dropped＝途中斷線、timeout＝等太久
        # （這兩種它可能還在背景做）；其他錯誤是空的
        self.kind = kind


def _net_kind(exc: httpx.HTTPError) -> str:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "offline"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)):
        return "dropped"
    return ""


@dataclass
class ScrapeResult:
    source: str = ""  # sync / manual
    started: float = 0.0
    finished: float = 0.0
    running: bool = False
    total: int = 0
    done: int = 0
    failed: int = 0
    no_image: int = 0  # 寫了 nfo 但沒有劇照：TMDB 沒有這集的圖，或 MoviePilot 下載圖片失敗
    stopped: bool = False  # 按了停止：送出去的做完，沒送的不送
    current: str = ""
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def episode_image(path: Path) -> Optional[Path]:
    """單集的劇照：MoviePilot 寫成和影片同名的圖片（X.jpg），也認 X-thumb.jpg。"""
    for stem in (path.stem, path.stem + "-thumb"):
        for ext in IMAGE_EXTS:
            f = path.with_name(stem + ext)
            if f.exists():
                return f
    return None


def series_tmdbid(series_dir: Path) -> Optional[str]:
    """劇集資料夾 tvshow.nfo 裡的 tmdbid。"""
    from .scanner import parse_nfo

    tmdb = str((parse_nfo(series_dir / "tvshow.nfo").get("provider_ids") or {}).get("Tmdb") or "")
    return tmdb if tmdb.isdecimal() else None


@dataclass
class FillResult:
    """補全缺集：替劇的每一季向 MoviePilot 建訂閱的結果。"""

    source: str = ""  # sync / manual
    started: float = 0.0
    finished: float = 0.0
    running: bool = False
    check: bool = False  # 只檢查（對照 TMDB 記下每一季缺哪幾集），不建訂閱
    total: int = 0  # 檢查的季數
    done: int = 0
    lacking: int = 0  # 只檢查時：缺集的季數
    created: int = 0  # 缺集，建了訂閱並請 MoviePilot 搜尋（兩個之間隔 fill_interval 秒）
    complete: int = 0  # 已播出的集都有，沒有建訂閱
    existing: int = 0  # 之前就訂閱過（不再請它搜，MoviePilot 自己會定時搜）
    sent: int = 0  # MoviePilot 已經找到資源、送去下載，還沒入庫的季：不重複訂閱
    missing: int = 0  # 缺的集數合計
    skipped: int = 0  # 沒有 tmdbid 的劇，以劇計
    excluded: int = 0  # 標了「不補」的劇，以劇計
    too_many: int = 0  # 缺的集數超過 fill_max_missing，不建訂閱的季
    absent: int = 0  # TMDB 上沒有的季（媒體庫的季號和 TMDB 對不上）：不建訂閱，要先整理季號
    failed: int = 0
    stopped: bool = False  # 按了停止：做完手上這一季就停
    current: str = ""
    details: List[str] = field(default_factory=list)  # 每一季的結果
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class UnsubscribeResult:
    """取消 MoviePilot 裡的訂閱：一個一個刪的進度。"""

    started: float = 0.0
    finished: float = 0.0
    running: bool = False
    total: int = 0
    done: int = 0
    failed: int = 0
    stopped: bool = False  # 按了停止：刪掉的就刪掉了，剩下的留著
    current: str = ""
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


MAX_UNSUB_ERRORS = 20
SUBS_CACHE_SECONDS = 60  # 清單上「已訂閱」的標記：MoviePilot 的訂閱清單記這麼久，不必每翻一頁、每打一個字都去問
TV_TYPE = "电视剧"  # MoviePilot 訂閱的類型
# MoviePilot 找到資源、交給下載器就把訂閱記成完成、移到歷史，這時集還在下載、還沒入庫。這麼久以內完成的訂閱算「已送下載」，
# 不重複訂閱；過了還沒入庫（下載失敗、種子被刪）就回到缺集，可以再補
# （網頁的提示和 wiki 寫的是「3 天」，改這裡要一起改）
SENT_GRACE_SECONDS = 3 * 86400
HISTORY_PAGE = 100  # 訂閱歷史一頁讀幾個（新的在前面，讀到超過 SENT_GRACE_SECONDS 的就停）
HISTORY_MAX_PAGES = 20


class MoviePilot:
    def __init__(
        self,
        cfg: MoviePilotConfig,
        config: Config,
        on_done: Optional[Callable[[List[str]], None]] = None,
        transport: Optional[httpx.BaseTransport] = None,
        db: Optional[Database] = None,
    ):
        self.cfg = cfg
        self.config = config
        self.on_done = on_done
        self.db = db
        self._no_image: Dict[str, float] = {}  # 沒有資料庫時（測試）記在記憶體
        self.verify_wait = 0.5  # MoviePilot 回報完成後等檔案出現（網路磁碟可能慢一點）
        self.result = ScrapeResult()
        self.fill_result = FillResult()
        self.unsubscribe_result = UnsubscribeResult()
        self._subs_cache: Optional[Tuple[float, Optional[List[dict]]]] = None  # (時間, 訂閱清單；讀不到是 None)
        self._sent_cache: Optional[Tuple[float, Dict[Tuple[int, int], float]]] = None  # (時間, 已送下載的季)
        self._lock = threading.Lock()
        self._fill_lock = threading.Lock()
        self._fill_excluded_lock = threading.Lock()
        self._clock = time.monotonic  # 測試換成假的時鐘
        self._plugin_checked = (0.0, False)  # 上次問外掛的時間、有沒有裝好啟用
        self._plugin_features: frozenset = frozenset()  # 外掛會哪些功能（1.1.0 起多了算名字 names）
        self._created_at: Optional[float] = None  # 補全缺集：上一個新訂閱建立的時間
        self._stop = threading.Event()
        self.workers = Workers(self._stop)  # 刮削、補全缺集；程式結束時停在兩項之間
        # 按了停止：刮削和補全缺集可能同時在跑，各停各的
        self._cancel = {"scrape": threading.Event(), "fill": threading.Event(), "unsubscribe": threading.Event()}
        self._transport = transport
        self._jwt: Optional[str] = None
        self._jwt_lock = threading.Lock()  # 登入 token 過期時只讓一個請求重新登入，其他的等它、用新的 token
        self._preview_ok_at = 0.0  # 上次確認 MoviePilot 夠新、支援整理預覽的時間

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.url and (self.cfg.api_token or (self.cfg.username and self.cfg.password)))

    # ---------------- HTTP ----------------

    def _client(self, timeout: float) -> httpx.Client:
        # 連線錯誤由 _request 轉成 MoviePilotError，順便分出連不上、途中斷線、等太久
        return httpx.Client(timeout=timeout, transport=self._transport)

    def _login(self, client: httpx.Client) -> str:
        resp = client.post(
            self.cfg.url.rstrip("/") + LOGIN_API, data={"username": self.cfg.username, "password": self.cfg.password}
        )
        if resp.status_code != 200:
            raise MoviePilotError(f"MoviePilot 帳號密碼登入失敗：HTTP {resp.status_code} {resp.text[:200]}")
        token = (resp.json() or {}).get("access_token")
        if not token:
            raise MoviePilotError("MoviePilot 登入後沒有回傳 token")
        return token

    def _post(self, path: str, body: dict, timeout: Optional[float] = None, query: Optional[dict] = None) -> dict:
        return self._request("POST", path, body, timeout, query)

    def _request(
        self, method: str, path: str, body: Optional[dict] = None, timeout: Optional[float] = None,
        query: Optional[dict] = None,
    ):
        """呼叫 MoviePilot 的 API，回傳 JSON；任何失敗（連不上、網址格式不對、HTTP 錯誤）都丟 MoviePilotError。

        被拒絕（401／403）而且有帳號密碼時登入一次再送；第二次還被拒絕就照一般的錯誤丟出去。
        """
        if not self.cfg.url:
            raise MoviePilotError("還沒設定 MoviePilot 網址")
        try:
            with self._client(timeout or self.cfg.timeout) as client:
                token = self._jwt
                resp = self._send(client, method, path, body, query, token)
                if resp.status_code in (401, 403) and self.cfg.username and self.cfg.password:
                    # 舊版 MoviePilot 的刮削 API 只認登入 token；或是之前的登入 token 過期了。
                    # 刮削是好幾個請求同時送：別的請求已經換了新 token 就直接用，不再登入一次
                    with self._jwt_lock:
                        if self._jwt == token:
                            self._jwt = self._login(client)
                        token = self._jwt
                    resp = self._send(client, method, path, body, query, token)
                return self._parse(resp, path)
        except httpx.InvalidURL as exc:  # 網址填錯（例如埠號不是數字）；不是 httpx.HTTPError
            raise MoviePilotError(f"MoviePilot 網址格式不對：{exc}") from exc
        except httpx.HTTPError as exc:
            message = describe(exc)
            if isinstance(exc, httpx.ConnectError) and urlsplit(self.cfg.url).port == 3000:
                message += FRONTEND_HINT
            raise MoviePilotError(message, kind=_net_kind(exc)) from exc

    def _send(self, client: httpx.Client, method: str, path: str, body: Optional[dict], query: Optional[dict],
              token: Optional[str]):
        headers, params = {}, dict(query or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif self.cfg.api_token:
            # 新版接受 X-API-KEY 標頭；token 查詢參數給接受 API 令牌的舊端點
            headers["X-API-KEY"] = self.cfg.api_token
            params["token"] = self.cfg.api_token
        return client.request(method, self.cfg.url.rstrip("/") + path, json=body, headers=headers, params=params)

    def _parse(self, resp: httpx.Response, path: str):
        """HTTP 錯誤換成看得懂的說明，成功時回傳 JSON。"""
        if resp.status_code in (401, 403):
            hint = "API 令牌不正確" if self.cfg.api_token else "請填 API 令牌"
            if self.cfg.api_token and not self.cfg.username:
                # V3 每個 API 都接受 API 令牌；舊版（V2）有些只接受帳號登入
                hint += "，或這個 MoviePilot 版本較舊、不接受 API 令牌：請改填 MoviePilot 的帳號密碼"
            raise MoviePilotError(f"MoviePilot 拒絕存取（HTTP {resp.status_code}）：{hint}", resp.status_code)
        if resp.status_code == 404:
            raise MoviePilotError(f"MoviePilot 沒有這個 API（{path}），請確認網址或升級 MoviePilot", 404)
        if resp.status_code in (502, 503, 504):  # 前面的反向代理連不到它
            raise MoviePilotError(f"MoviePilot 回應 HTTP {resp.status_code}：它前面的代理連不到它（沒在執行、正在重新啟動，或忙到沒回應）",
                                  resp.status_code, kind="offline")
        if resp.status_code >= 400:
            if resp.text.lstrip().startswith("<"):
                raise MoviePilotError(f"MoviePilot 回應 HTTP {resp.status_code}，內容是網頁不是 API，請確認網址",
                                      resp.status_code)
            raise MoviePilotError(f"MoviePilot 回應 HTTP {resp.status_code}：{resp.text[:200]}", resp.status_code)
        try:
            return resp.json()
        except ValueError:
            raise MoviePilotError("MoviePilot 回應不是 JSON，請確認網址是 MoviePilot")

    # ---------------- 功能 ----------------

    def unmap_path(self, path: str) -> str:
        """MoviePilot 看到的路徑轉回 Mi302 的路徑（MoviePilot 通知媒體伺服器時用的是它自己的路徑）。"""
        for rule in sorted(self.cfg.path_mappings, key=lambda r: len(r.target), reverse=True):
            dst = rule.target.rstrip("/")
            if path == dst or path.startswith(dst + "/"):
                return rule.source.rstrip("/") + path[len(dst):]
        return path

    def map_path(self, path: str) -> str:
        """Mi302 的路徑轉成 MoviePilot 看到的路徑（最長前綴優先）。"""
        for rule in sorted(self.cfg.path_mappings, key=lambda r: len(r.source), reverse=True):
            src = rule.source.rstrip("/")
            if path == src or path.startswith(src + "/"):
                return rule.target.rstrip("/") + path[len(src):]
        return path

    # ---------------- Mi302 整理助手外掛 ----------------

    def rename_plugin_ready(self) -> bool:
        """MoviePilot 裝好、啟用了 Mi302 整理助手外掛，而且設定裡沒關掉；5 分鐘內問過就不再問。"""
        if not self.cfg.rename_plugin:
            return False
        now = time.time()
        if now - self._plugin_checked[0] < 300:
            return self._plugin_checked[1]
        try:
            res = self._request("GET", PLUGIN_API + "/status", timeout=10)
            ready = isinstance(res, dict) and bool(res.get("enabled"))
            features = res.get("features") if ready and isinstance(res.get("features"), list) else []
        except MoviePilotError:
            ready, features = False, []  # 沒裝（404）、連不上：照舊走整理
        self._plugin_checked = (now, ready)
        self._plugin_features = frozenset(str(f) for f in features)
        return ready

    def naming_plugin_ready(self) -> bool:
        """外掛會照 MoviePilot 的規則算名字（1.1.0 起）：預覽不必跑它的整理預覽。"""
        return self.rename_plugin_ready() and "names" in self._plugin_features

    def plugin_names(self, items: List[dict], tmdbid: Optional[str], season: Optional[int], episode_format: Optional[str],
                     mtype: Optional[str], timeout: float) -> List[dict]:
        """請外掛照 MoviePilot 的規則算這些檔案整理後的名字（相對於媒體庫目錄），只算不改。items：[{path, fileid, size}]。
        回傳每個要整理的檔案一筆，格式和它的整理預覽一樣（source、target、success、message、title、type、season、episode），
        target 是相對路徑；nfo、圖片這些它整理時不管的不回。"""
        body = {"items": items, "tmdbid": tmdbid or "", "season": season, "episode_format": episode_format or "",
                "type": mtype or ""}
        res = self._request("POST", PLUGIN_API + "/names", body, timeout=timeout)
        if not (isinstance(res, dict) and res.get("success") and isinstance(res.get("items"), list)):
            raise MoviePilotError(str((res or {}).get("message") or "Mi302 整理助手沒有算出名字"))
        return [{"source": r.get("path"), **{k: r.get(k) for k in ("target", "success", "message", "title", "type",
                                                                  "season", "episode")}}
                for r in res["items"] if isinstance(r, dict) and r.get("path")]

    def plugin_rename(self, items: List[dict]) -> str:
        """請外掛在背景照順序改名，回傳工作 id。"""
        res = self._request("POST", PLUGIN_API + "/rename", {"items": items}, timeout=30)
        if not (isinstance(res, dict) and res.get("success") and res.get("job")):
            raise MoviePilotError(str((res or {}).get("message") or "Mi302 整理助手沒有接下改名"))
        return str(res["job"])

    def plugin_job(self, job_id: str) -> dict:
        res = self._request("GET", PLUGIN_API + "/job", timeout=30, query={"id": job_id})
        if not (isinstance(res, dict) and res.get("success")):
            raise MoviePilotError(str((res or {}).get("message") or "查不到改名工作"))
        return res

    def plugin_cancel(self, job_id: str) -> None:
        try:
            self._request("POST", PLUGIN_API + "/cancel", timeout=15, query={"id": job_id})
        except MoviePilotError as exc:
            log.warning("請 Mi302 整理助手停止失敗：%s", exc)

    def reachable(self) -> bool:
        """MoviePilot 有沒有在回應（回什麼都算，連不上、等太久才不算）。"""
        try:
            self._request("GET", SYSTEM_ENV_API, timeout=15)
        except MoviePilotError as exc:
            return not exc.kind
        return True

    def test(self) -> dict:
        """用空路徑呼叫刮削 API 驗證網址與令牌。

        MoviePilot 會以「刮削路径无效」拒絕空路徑，所以不會真的刮削；那是預期的回應，不是路徑設定錯了。
        """
        try:
            self._post(SCRAPE_API, {"storage": "local", "type": "file", "path": ""}, timeout=15)
        except MoviePilotError as exc:
            return {"ok": False, "message": str(exc)}
        return {
            "ok": True,
            "message": "連線成功，MoviePilot 接受了 API 令牌。測試用空路徑，不會真的刮削；"
            "兩邊路徑對不對得上，要看第一次刮削的結果。",
        }

    def scrape_one(self, path: Path, is_dir: bool) -> Tuple[bool, str]:
        mp_path = self.map_path(str(path))
        item = {
            "storage": "local",
            "type": "dir" if is_dir else "file",
            "path": mp_path + ("/" if is_dir and not mp_path.endswith("/") else ""),
            "name": path.name,
            "basename": path.name if is_dir else path.stem,
        }
        if not is_dir:
            item["extension"] = path.suffix.lstrip(".").lower()
        query = {}
        tmdbid = None if is_dir else self._episode_tmdbid(path)
        if tmdbid:
            # 已經刮削過的劇：直接說是哪一部，MoviePilot 不必用檔名搜尋 TMDB，也不會認錯（V3 才有這些參數，舊版會忽略）
            query = {"media_source": "themoviedb", "media_id": tmdbid, "type_name": "电视剧"}
        body = self._post(SCRAPE_API, item, query=query)
        ok = bool(body.get("success"))
        message = body.get("message") or ("完成" if ok else "失敗")
        if not ok and "不存在" in message:
            message += f"（MoviePilot 找不到 {mp_path}，請檢查路徑對應）"
        return ok, message

    def _series_dir(self, path: Path) -> Optional[Path]:
        """劇集媒體庫裡一集所屬的劇集資料夾；不是劇集時回傳 None。

        和掃描器用同一套判斷，媒體庫底下有分類資料夾（电视剧/国产剧/庆余年 (2019)）時，
        送的是那一部劇，不會把整個分類當成一部劇送出去。
        """
        ltype, root = self._library_of(path)
        if ltype != "tvshows" or root is None:
            return None
        return series_folder(root, path)

    def _episode_tmdbid(self, path: Path) -> Optional[str]:
        series = self._series_dir(path)
        return series_tmdbid(series) if series else None

    def check_output(self, path: Path, is_dir: bool) -> Tuple[str, str]:
        """MoviePilot 回報完成之後，看它有沒有真的寫出 nfo 和劇照。

        回傳 (ok / no_nfo / no_image, 說明)。認不出集數時 MoviePilot 什麼都不寫，還是回報完成。
        """
        want = path / "tvshow.nfo" if is_dir else path.with_suffix(".nfo")
        for attempt in range(3):
            if want.exists():
                break
            if attempt < 2:
                self._stop.wait(self.verify_wait)
        else:
            if is_dir:
                return "no_nfo", "MoviePilot 說完成，但沒有寫出 tvshow.nfo（可能認不出這部劇）"
            return "no_nfo", "MoviePilot 說完成，但沒有寫出 nfo（多半是認不出集數，檔名要有 S01E01 這類集號）"
        if not is_dir and self._series_dir(path) and not episode_image(path):
            return "no_image", "有 nfo 但沒有劇照"
        return "ok", ""

    # ---------------- 沒有劇照的集 ----------------

    def _mark_no_image(self, path: Path) -> None:
        now = time.time()
        if self.db is None:
            self._no_image[str(path)] = now
            return
        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO mp_no_image(path, at) VALUES(?, ?) ON CONFLICT(path) DO UPDATE SET at=excluded.at",
                (str(path), int(now)),
            )
            # 超過重送間隔的紀錄已經不擋重送了，順手清掉，免得這張表一直長大（at 有索引）
            self.db.conn.execute("DELETE FROM mp_no_image WHERE at<?", (int(now - NO_IMAGE_RETRY_SECONDS),))
            self.db.conn.commit()

    def _recently_no_image(self) -> Set[str]:
        since = time.time() - NO_IMAGE_RETRY_SECONDS
        if self.db is None:
            return {p for p, at in self._no_image.items() if at >= since}
        return {r["path"] for r in self.db.query("SELECT path FROM mp_no_image WHERE at>=?", (int(since),))}

    # ---------------- 決定要送哪些路徑 ----------------

    def _library_of(self, path: Path) -> Tuple[Optional[str], Optional[Path]]:
        best: Tuple[Optional[str], Optional[Path]] = (None, None)
        for lib in self.config.libraries:
            for root in lib.paths:
                r = Path(root).expanduser()
                if (path == r or r in path.parents) and (best[1] is None or len(str(r)) > len(str(best[1]))):
                    best = (lib.type, r)
        return best

    def plan(self, paths: Iterable[str], with_images: bool = False) -> List[Tuple[Path, bool]]:
        """把影片（strm）路徑轉成要送去刮削的清單 [(路徑, 是否資料夾)]，略過已經有 nfo 的。

        with_images：已經有 nfo 卻沒有劇照的集也送（手動刮削用）；最近送過確定沒有劇照的不送。
        """
        out: List[Tuple[Path, bool]] = []
        seen = set()
        no_image = self._recently_no_image() if with_images else set()

        def add(p: Path, is_dir: bool):
            if p not in seen:
                seen.add(p)
                out.append((p, is_dir))

        for raw in paths:
            p = Path(raw)
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            has_nfo = p.with_suffix(".nfo").exists()
            ltype, root = self._library_of(p)
            if ltype == "tvshows" and root is not None:
                if has_nfo and not (with_images and str(p) not in no_image and not episode_image(p)):
                    continue
                series = self._series_dir(p)
                if series is not None:
                    if series in seen:
                        continue
                    if not (series / "tvshow.nfo").exists():
                        add(series, True)  # 還沒刮削過的劇：整部劇送一次
                        continue
                add(p, False)
            else:
                # 單片資料夾裡的 movie.nfo 也算已刮削
                if has_nfo or (root is not None and p.parent != root and (p.parent / "movie.nfo").exists()):
                    continue
                add(p, False)
        return out

    def missing(self) -> List[str]:
        """媒體庫裡所有影片；已經有 nfo 的會在 plan() 濾掉。"""
        out: List[str] = []
        for lib in self.config.libraries:
            for root in lib.paths:
                for dirpath, dirnames, filenames in os.walk(Path(root).expanduser()):
                    dirnames.sort()
                    for name in sorted(filenames):
                        if Path(name).suffix.lower() in VIDEO_EXTS:
                            out.append(os.path.join(dirpath, name))
        return out

    # ---------------- 執行 ----------------

    @property
    def concurrency(self) -> int:
        return max(1, min(int(self.cfg.concurrency or 1), MAX_CONCURRENCY))

    def scrape(self, paths: Iterable[str], source: str, with_images: bool = False) -> ScrapeResult:
        if not self._lock.acquire(blocking=False):
            log.info("MoviePilot 刮削已在進行，略過")
            return self.result
        r = self.result = ScrapeResult(source=source, started=time.time(), running=True)
        self._cancel["scrape"].clear()
        scraped: Dict[int, str] = {}
        abort = threading.Event()
        count = threading.Lock()

        def one(index: int, path: Path, is_dir: bool) -> None:
            if abort.is_set() or self._stop.is_set() or self._cancel["scrape"].is_set():  # 程式要結束、按了停止：沒送的下次再送
                return
            r.current = str(path)
            try:
                ok, message = self.scrape_one(path, is_dir)
                kind, note = self.check_output(path, is_dir) if ok else ("failed", message)
            except MoviePilotError as exc:
                # 連線或認證錯誤，後面的也不會成功；同時在跑的幾項只記一次
                with count:
                    if not abort.is_set():
                        abort.set()
                        r.errors.append(str(exc))
                        log.error("MoviePilot 刮削中止：%s", exc)
                return
            except Exception as exc:  # 單一項目的意外錯誤不影響其他項目
                log.exception("刮削 %s 時發生錯誤", path)
                kind, note = "failed", f"{type(exc).__name__}: {exc}"
            with count:
                if kind in ("ok", "no_image"):
                    r.done += 1
                    scraped[index] = str(path)
                    if kind == "no_image":
                        r.no_image += 1
                        self._mark_no_image(path)
                        log.info("MoviePilot 刮削 %s：%s", path.name, note)
                else:
                    r.failed += 1
                    r.errors.append(f"{path.name}：{note}")
                    log.warning("MoviePilot 刮削失敗 %s：%s", path, note)

        try:
            items = self.plan(paths, with_images)
            r.total = len(items)
            log.info("送 %s 個項目給 MoviePilot 刮削（同時 %s 項）", len(items), self.concurrency)
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                for future in [pool.submit(one, i, p, d) for i, (p, d) in enumerate(items)]:
                    future.result()
            if abort.is_set():
                r.failed = r.total - r.done  # 中止後沒做的都算失敗
            elif self._cancel["scrape"].is_set() and not self._stop.is_set():
                r.stopped = True
                left = r.total - r.done - r.failed
                if left:
                    r.errors.append(f"按了停止，還有 {left} 項沒送，下次刮削再送")
        finally:
            r.running = False
            r.current = ""
            r.finished = time.time()
            self._lock.release()
        done = [scraped[i] for i in sorted(scraped)]
        if done and self.on_done:
            self.on_done(done)  # 只重新掃描刮削過的地方
        return r

    def scrape_in_background(self, paths: Optional[List[str]], source: str) -> bool:
        """paths 為 None 時送媒體庫裡所有還沒有 nfo 的影片。"""
        if self._lock.locked():
            return False

        def job():
            # 手動刮削整個媒體庫時，有 nfo 卻沒有劇照的集也再送一次
            self.scrape(self.missing() if paths is None else paths, source, with_images=paths is None)

        self.workers.start(job)
        return True

    def stop(self) -> None:
        """程式關閉時：刮削、補全缺集做完手上這一項就停。"""
        self._stop.set()

    # ---------------- 補全缺集 ----------------

    def tmdb_episodes(self, tmdbid: int, season: int, strict: bool = False) -> Optional[Dict[int, str]]:
        """TMDB 上這一季的集號 → 播出日期（沒填是空字串），透過 MoviePilot 查；查不到時回傳 None。
        TMDB 上根本沒有這一季（媒體庫的季號和 TMDB 對不上）時回傳空的 {}。
        TMDB_FRESH_SECONDS 以內查過的直接用記下的；查到的（包括「沒有這一季」）記進 tmdb_seasons，補全缺集的清單用它算缺哪幾集。
        strict：MoviePilot 連不上、拒絕存取時丟 MoviePilotError（只檢查時用：後面的也查不到，整批停下）。"""
        saved = self._saved_episodes(tmdbid, season)
        if saved is not None:
            return saved
        try:
            body = self._request("GET", TMDB_EPISODES_API.format(tmdbid=tmdbid, season=season), timeout=30)
        except MoviePilotError as exc:
            if strict and (exc.kind or exc.status in (401, 403)):
                raise
            log.warning("向 MoviePilot 查 TMDB %s 第 %s 季的集數失敗：%s", tmdbid, season, exc)
            return None
        items = body.get("data") if isinstance(body, dict) else body  # V3 包在 data 裡，V2 直接是清單
        episodes = {
            int(e["episode_number"]): str(e.get("air_date") or "")[:10]
            for e in items or [] if isinstance(e, dict) and str(e.get("episode_number") or "").isdecimal()
        }
        # 這一季一集都沒有：MoviePilot 對不存在的季也回成功和空清單，TMDB 暫時出錯時也可能是空的，
        # 再看這部劇有哪幾季，確定沒有這一季才記成「沒有」
        if not episodes and not self._season_absent(tmdbid, season):
            return None
        if self.db is not None:
            self.db.execute(
                "INSERT INTO tmdb_seasons(tmdbid, season, episodes, at) VALUES(?,?,?,?) "
                "ON CONFLICT(tmdbid, season) DO UPDATE SET episodes=excluded.episodes, at=excluded.at",
                (int(tmdbid), int(season), json.dumps(episodes), int(time.time())))
        return episodes

    def _season_absent(self, tmdbid: int, season: int) -> bool:
        """TMDB 上這部劇確定沒有這一季：查得到它有哪幾季，裡面沒有這一季。查不到、一季都沒有（劇不存在、TMDB 出錯）
        回傳 False（不確定）。"""
        try:
            body = self._request("GET", TMDB_SEASONS_API.format(tmdbid=tmdbid), timeout=30)
        except MoviePilotError as exc:
            log.warning("向 MoviePilot 查 TMDB %s 有哪幾季失敗：%s", tmdbid, exc)
            return False
        items = body.get("data") if isinstance(body, dict) else body
        numbers = {int(s["season_number"]) for s in items or [] if isinstance(s, dict)
                   and str(s.get("season_number") if s.get("season_number") is not None else "").isdecimal()}
        return bool(numbers) and int(season) not in numbers

    def _saved_episodes(self, tmdbid: int, season: int) -> Optional[Dict[int, str]]:
        """剛查過（TMDB_FRESH_SECONDS 以內）記下的這一季（TMDB 沒有這一季是空的 {}）；沒有、太舊、壞掉、沒有資料庫回傳 None。"""
        if self.db is None:
            return None
        row = self.db.one("SELECT episodes, at FROM tmdb_seasons WHERE tmdbid=? AND season=?", (int(tmdbid), int(season)))
        if not row or time.time() - row["at"] > TMDB_FRESH_SECONDS:
            return None
        return _load_episodes(row["episodes"])

    def subscribe(self, name: str, year: Optional[int], tmdbid: int, season: int) -> Tuple[str, str, Optional[int]]:
        """替一季劇向 MoviePilot 建訂閱。

        回傳 (結果, MoviePilot 的訊息, 訂閱 id)：created = 建了訂閱、existing = 之前就訂閱過、
        complete = 媒體庫已經齊全（舊版會這樣拒絕）、failed = 其他失敗。
        """
        body = {
            "name": name, "year": str(year) if year else None, "type": "电视剧", "season": season,
            # V3 用 media_source + media_id 指定是哪一部；V2 認 tmdbid。另一邊不認的欄位會被忽略
            "media_source": "themoviedb", "media_id": str(tmdbid), "tmdbid": tmdbid,
        }
        res = self._post(SUBSCRIBE_API, body, timeout=60)
        message = str(res.get("message") or "")
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        sid = int(data["id"]) if str(data.get("id") or "").isdecimal() and int(data["id"]) else None
        if "订阅已存在" in message or "訂閱已存在" in message:  # V3 對已存在的訂閱也回 success
            return "existing", message, sid
        if res.get("success"):
            return "created", message or "已建立訂閱", sid
        if "已存在" in message:  # 舊版：媒体库中已存在
            return "complete", message, None
        return "failed", message or "MoviePilot 沒有說明原因", None

    # ---------------- 手動整理 ----------------

    def transfer(
        self, fileitems: List[dict], tmdbid: Optional[str], season: Optional[int], episode_format: Optional[str],
        scrape: bool, target_path: Optional[str], preview: bool, mtype: Optional[str] = "电视剧",
        timeout: Optional[float] = None, single: bool = False, reorganize: bool = False,
    ) -> List[dict]:
        """請 MoviePilot 整理這些 115 上的檔案（一次一批、同一個集數定位模板）。

        preview=True 只預覽新路徑；否則真的在 115 上移動、改名（和刮削）。tmdbid、season、mtype（电视剧／电影）
        空的時候讓 MoviePilot 自己辨識。target_path 是空的時候照 MoviePilot 的目錄設定放；有給就放在那個資料夾
        底下（不另加類型、類別資料夾）。回傳每個檔案的結果：source、target、success、message、episode、state。
        single=True 時只送一個項目（fileitem），和 MoviePilot 網頁整理一個資料夾一樣：資料夾裡的影片、字幕、音軌都整理。
        它網頁整理對話框的「按類型分類」「按類別分類」「複用歷史識別信息」（library_type_folder、library_category_folder、
        from_history）一律關掉：留在原本的分類資料夾裡，只改資料夾和檔名；「刮削元數據」（scrape）照參數，整理 115 網盤送 False。
        reorganize=True：MoviePilot 有成功整理過的紀錄時，和它的網頁一樣清掉舊紀錄重新整理；
        沒有的話它會當成「已整理過」跳過（預覽不看紀錄，所以預覽時看不出來）。
        """
        body = {"transfer_type": "move", "scrape": scrape, "preview": preview,
                "library_type_folder": False, "library_category_folder": False, "from_history": False}
        if single and len(fileitems) == 1:
            body["fileitem"] = fileitems[0]
        else:
            body["fileitems"] = fileitems
        if tmdbid:
            body.update(media_source="themoviedb", media_id=str(tmdbid), tmdbid=int(tmdbid))  # V3 看前兩個，V2 看 tmdbid
        if mtype:
            body["type_name"] = mtype
        if season is not None:
            body["season"] = season
        if episode_format:
            body["episode_format"] = episode_format
        if target_path:
            body.update(target_storage="u115", target_path=target_path)
        if reorganize:
            body["reorganize"] = True
        res = self._request("POST", TRANSFER_API, body, timeout=timeout, query={"background": "false"})
        data = res.get("data") if isinstance(res, dict) else None
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            if not preview and isinstance(res, dict) and res.get("success"):
                # V2 真的整理時只回 {"success": true}（同步做完才回應），沒有每個檔案的結果
                return [{"source": fi.get("path"), "success": True, "state": "completed",
                         "message": "MoviePilot 沒有回傳每個檔案的結果"} for fi in fileitems]
            raise MoviePilotError(str((res or {}).get("message") or "MoviePilot 沒有回傳整理結果"))
        return [i for i in items if isinstance(i, dict)]

    def library_dirs(self) -> List[dict]:
        """MoviePilot 的目錄設定（下載目錄 → 媒體庫目錄、類型／類別資料夾、覆蓋模式…）；要管理員帳號。
        V3 是 /storage/directories，V2 放在系統設定 Directories。"""
        try:
            res = self._request("GET", DIRECTORIES_API, query={"directory_type": "all"}, timeout=30)
            data = res.get("data") if isinstance(res, dict) else res
        except MoviePilotError as exc:
            if exc.status != 404:
                raise
            res = self._request("GET", DIRECTORIES_V2_API, timeout=30)
            data = (res.get("data") or {}).get("value") if isinstance(res, dict) else None
        return [d for d in data or [] if isinstance(d, dict)] if isinstance(data, list) else []

    def transfer_target(self, fileitem: dict) -> Optional[dict]:
        """MoviePilot 自己照目錄設定會把這個項目整理到哪個媒體庫目錄（和它的網頁整理對話框一樣問）；
        對不上任何一個目錄設定時回傳 None。"""
        try:
            res = self._request("POST", TRANSFER_TARGET_API, {"fileitem": fileitem, "target_storage": None}, timeout=30)
        except MoviePilotError as exc:
            if exc.status in (404, 405):
                return None
            raise
        data = res.get("data") if isinstance(res, dict) else None
        return data if isinstance(data, dict) and data.get("target_path") else None

    def transfer_history(self, fileitems: List[dict]) -> int:
        """這些項目（資料夾會往下找）在 MoviePilot 有幾條成功整理的紀錄；舊版沒有這個 API 時回 0。"""
        body = {"fileitem": fileitems[0]} if len(fileitems) == 1 else {"fileitems": fileitems}
        try:
            res = self._request("POST", TRANSFER_HISTORY_API, body, timeout=60)
        except MoviePilotError as exc:
            if exc.status in (404, 405):
                return 0
            raise
        data = res.get("data") if isinstance(res, dict) else None
        return int(data.get("history_count") or 0) if isinstance(data, dict) and data.get("reorganize") else 0

    def transfer_name(self, path: str, filetype: str) -> Tuple[bool, str]:
        """MoviePilot 整理這個 115 路徑後會叫什麼：filetype=dir 回傳媒體資料夾名稱，file 回傳檔名。
        它用自己的辨識和重命名格式算，不動任何檔案。認不出來時回傳 (False, 說明)；連不上等錯誤丟 MoviePilotError。"""
        res = self._request("GET", TRANSFER_NAME_API, query={"path": path, "filetype": filetype}, timeout=60)
        data = res.get("data") if isinstance(res, dict) else None
        if isinstance(res, dict) and res.get("success") and isinstance(data, dict) and data.get("name"):
            return True, str(data["name"])
        return False, str((res or {}).get("message") or "MoviePilot 認不出來")

    def rename_formats(self) -> Tuple[str, str]:
        """MoviePilot 的重命名格式（劇集、電影）；要管理員帳號。沒有回的是空字串。"""
        res = self._request("GET", SYSTEM_ENV_API, timeout=15)
        data = res.get("data") if isinstance(res, dict) else None
        if not isinstance(data, dict):
            raise MoviePilotError(str((res or {}).get("message") or "MoviePilot 沒有回傳系統設定"))
        return str(data.get("TV_RENAME_FORMAT") or ""), str(data.get("MOVIE_RENAME_FORMAT") or "")

    def check_transfer_preview(self) -> None:
        """確認 MoviePilot 支援整理預覽，不支援就丟出 MoviePilotError。十分鐘內確認過就不再問。

        舊版的手動整理 API 會忽略 preview 直接整理，所以版本查不到也當成不支援。
        """
        if time.time() - self._preview_ok_at < 600:
            return
        need = "v2.11.1-1"
        try:
            res = self._request("GET", SYSTEM_ENV_API, timeout=15)
        except MoviePilotError as exc:
            if exc.kind:  # 連不上：不是版本的問題
                raise
            raise MoviePilotError(f"查不到 MoviePilot 的版本（{exc}）。舊版的整理不支援預覽、會直接執行，"
                                  f"所以確認版本之前不預覽；整理需要 MoviePilot {need} 以上，帳號要是管理員")
        data = res.get("data") if isinstance(res, dict) else None
        version = str(data.get("VERSION") or "") if isinstance(data, dict) else ""
        parsed = parse_version(version)
        if not parsed or parsed < PREVIEW_MIN_VERSION:
            raise MoviePilotError(f"MoviePilot {version or '版本不明'} 太舊：手動整理的預覽從 {need} 開始，"
                                  "更舊的版本會忽略預覽直接整理。請先升級 MoviePilot")
        self._preview_ok_at = time.time()

    def recommend_format(self, fileitems: List[dict]) -> Tuple[Optional[str], str]:
        """請 MoviePilot 依檔名推薦集數定位模板；回傳 (模板, 說明)，推薦不出來時模板是 None。"""
        try:
            res = self._request("POST", EPISODE_FORMAT_API, {"fileitems": fileitems}, timeout=60)
        except MoviePilotError as exc:
            return None, str(exc)
        data = res.get("data") if isinstance(res, dict) else None
        if isinstance(res, dict) and res.get("success") and isinstance(data, dict) and data.get("episode_format"):
            return str(data["episode_format"]), str(data.get("rule_name") or data.get("reason") or "")
        return None, str((res or {}).get("message") or "MoviePilot 推薦不出集數定位模板")

    def search_subscription(self, sid: int) -> str:
        """請 MoviePilot 馬上搜尋這條訂閱（V3 是 POST，V2 是 GET）；回傳它的說明。"""
        path = SUBSCRIBE_SEARCH_API.format(sid=sid)
        try:
            res = self._request("POST", path, timeout=30)
        except MoviePilotError as exc:
            if "HTTP 405" not in str(exc):
                raise
            res = self._request("GET", path, timeout=30)
        if isinstance(res, dict) and res.get("success") is False:
            raise MoviePilotError(str(res.get("message") or "MoviePilot 沒有安排搜尋"))
        return str((res or {}).get("message") or "已安排搜尋") if isinstance(res, dict) else "已安排搜尋"

    def fill(self, series: List[dict], source: str, check: bool = False, force: bool = False) -> FillResult:
        """替這些劇（library_series 的格式）的每一季建訂閱，一季一季來。
        check=True：只對照 TMDB、記下每一季缺哪幾集（清單就看得出誰真的缺），不建訂閱。
        MoviePilot 已經在處理的季（還訂閱著，或剛送去下載、還沒入庫）不重複訂閱；force=True 照樣送（「再補一次」）。"""
        if not self._fill_lock.acquire(blocking=False):
            log.info("補全缺集已在進行，略過")
            return self.fill_result
        self.fill_result = r = FillResult(source=source, started=time.time(), running=True, check=check)
        self._cancel["fill"].clear()
        self._created_at = None
        try:
            if not self.enabled:
                raise MoviePilotError("還沒設定 MoviePilot：對照 TMDB、建訂閱都是透過它")
            jobs: List[Tuple[dict, dict]] = []
            excluded = self.fill_excluded()
            for show in series:
                if not show.get("tmdbid"):
                    r.skipped += 1
                    r.details.append(f"{show['name']}：沒有 tmdbid，略過（先刮削）")
                    continue
                if str(show["tmdbid"]) in excluded:
                    r.excluded += 1
                    r.details.append(f"{show['name']}：標了「不補」，略過")
                    continue
                jobs += [(show, x) for x in show.get("seasons") or []]
            r.total = len(jobs)
            log.info("%s：檢查 %s 季", "檢查缺集" if check else "補全缺集", len(jobs))
            handed = None if check or force else (self.subscribed_seasons() or set(), self.sent_seasons())
            for show, info in jobs:
                if self._stop.is_set() or self._cancel["fill"].is_set():
                    r.stopped = not self._stop.is_set()
                    if r.stopped:
                        r.details.append(f"按了停止，還有 {r.total - r.done} 季沒送")
                    break  # 程式要結束、按了停止
                label = f"{show['name']} S{info['season']:02d}"
                r.current = label
                try:
                    if not self._fill_season(r, show, info, label, check, handed):
                        r.stopped = not self._stop.is_set()
                        if r.stopped:
                            r.details.append(f"按了停止，還有 {r.total - r.done} 季沒送")
                        break  # 等的時候程式要結束、按了停止
                except MoviePilotError as exc:
                    # 連線或認證錯誤，後面的也不會成功
                    r.failed += r.total - r.done
                    r.errors.append(str(exc))
                    log.error("補全缺集中止：%s", exc)
                    break
                r.done += 1
            if not r.errors:  # 做完（或停下）寫一行，日誌上看得出結束了
                log.info("%s%s：%s / %s 季，缺集的 %s 季", "檢查缺集" if check else "補全缺集", "停止" if r.stopped else "完成",
                         r.done, r.total, r.lacking if check else r.created + r.existing + r.sent + r.too_many)
        except MoviePilotError as exc:
            r.errors.append(str(exc))
        finally:
            r.running = False
            r.current = ""
            r.finished = time.time()
            self._subs_cache = self._sent_cache = None  # 訂閱變了：清單上的「已訂閱」「已送下載」重新讀
            self._fill_lock.release()
        return r

    def _pace(self, r: FillResult, label: str) -> bool:
        """上一個新訂閱建立還不到 fill_interval 秒就先等：每個新訂閱都會讓 MoviePilot 把所有站點搜一遍，
        一口氣建幾百個，站點會被連續請求、被 Cloudflare 擋。程式要結束回傳 False。"""
        interval = max(0.0, float(self.cfg.fill_interval or 0))
        if not interval or self._created_at is None:
            return True
        wait = self._created_at + interval - self._clock()
        if wait <= 0:
            return True
        r.current = f"{label}（隔 {int(interval)} 秒再建下一個訂閱，免得站點被 Cloudflare 擋）"
        end = self._clock() + wait
        while not self._cancel["fill"].is_set():  # 等的時候按了停止也要馬上醒來
            left = end - self._clock()
            if left <= 0:
                return True
            if self._stop.wait(min(1.0, left)):
                return False
        return False

    def _fill_season(self, r: FillResult, show: dict, info: dict, label: str, check: bool = False,
                     handed: Optional[Tuple[Set[Tuple[int, int]], Dict[Tuple[int, int], float]]] = None) -> bool:
        """一季：查 TMDB 已播出的集 → 缺集才建訂閱 → 新建的請 MoviePilot 搜尋（之前就訂閱過的不再搜，
        它自己會定時搜）。check：只記下缺哪幾集，不建訂閱。handed：MoviePilot 已經在處理的季（還訂閱著的、
        已送下載的），不重複訂閱。等的時候程式要結束回傳 False。"""
        tmdbid, season = int(show["tmdbid"]), int(info["season"])
        episodes = self.tmdb_episodes(tmdbid, season, strict=check)
        if episodes == {}:
            # MoviePilot 查不到這一季的總集數，訂閱一定被拒（「未获取到第 N 季的总集数」），每次補全都會再失敗一次
            r.absent += 1
            r.details.append(f"{label}：TMDB 沒有第 {season} 季，媒體庫的季號可能和 TMDB 不同；不建訂閱，"
                             "先到「115 網盤 → 整理 115 網盤」修正季號")
            return True
        missing: List[int] = []
        unsure = ""
        if episodes is not None:
            have = set(range(info["first"], info["last"] + 1)) - set(info.get("gaps") or []) if info.get("count") else set()
            missing, undated, aired = season_missing(episodes, have)
            if undated:
                unsure = f"另有 {len(undated)} 集（{ep_ranges(undated)}）TMDB 沒有播出日期，不確定播了沒，沒算進去"
            if not missing:
                r.complete += 1
                r.details.append(f"{label}：TMDB 已播出的 {aired} 集都有" + ("" if check else "，不建訂閱")
                                 + (f"；{unsure}" if unsure else ""))
                return True
        if check:
            if episodes is None:
                r.failed += 1
                r.details.append(f"{label}：查不到 TMDB 的集數")
            else:
                r.lacking += 1
                r.missing += len(missing)
                r.details.append(f"{label}：缺 {len(missing)} 集（{ep_ranges(missing)}）" + (f"；{unsure}" if unsure else ""))
            return True
        if episodes is not None:
            limit = int(self.cfg.fill_max_missing or 0)
            if limit and len(missing) > limit:
                r.too_many += 1
                r.details.append(f"{label}：缺 {len(missing)} 集（{ep_ranges(missing)}），超過「缺超過幾集的季不補」的 "
                                 f"{limit} 集，不建訂閱")
                return True
            r.missing += len(missing)
        lack = f"缺 {len(missing)} 集（{ep_ranges(missing)}）" if missing else "查不到 TMDB 集數，交給 MoviePilot 判斷"
        if handed and self._already_handed(r, (tmdbid, season), f"{label}：{lack}", episodes or {}, missing, handed):
            return True
        if not self._pace(r, label):
            return False
        self._subscribe_season(r, show, season, label, lack, unsure)
        return True

    def _subscribe_season(self, r: FillResult, show: dict, season: int, label: str, lack: str, unsure: str) -> None:
        """替缺集的這一季建訂閱，新建的請 MoviePilot 馬上搜尋；結果記進 r。"""
        r.current = label
        outcome, message, sid = self.subscribe(show["name"], show.get("year"), int(show["tmdbid"]), season)
        setattr(r, outcome, getattr(r, outcome) + 1)
        if outcome == "failed":
            r.details.append(f"{label}：{lack}；{message}")
            r.errors.append(f"{label}：{message}")
            log.warning("MoviePilot 不接受訂閱 %s：%s", label, message)
            return
        note = message
        if outcome == "existing":
            note += "；之前就訂閱過，MoviePilot 會在定時搜尋時處理"
        else:  # 新建的：沒回 id（舊版）也算，下一個照樣要隔開
            self._created_at = self._clock()
        if outcome == "created" and sid:
            try:
                note += "；" + self.search_subscription(sid)
            except MoviePilotError as exc:
                note += f"；沒有安排到搜尋（{exc}），MoviePilot 會在定時搜尋時處理"
                log.warning("請 MoviePilot 搜尋訂閱 %s 失敗：%s", sid, exc)
        if unsure:
            note += f"；{unsure}"
        r.details.append(f"{label}：{lack}；{note}")
        log.info("補全缺集 %s：%s；%s", label, lack, note)

    @staticmethod
    def _already_handed(r: FillResult, key: Tuple[int, int], text: str, episodes: Dict[int, str], missing: List[int],
                        handed: Tuple[Set[Tuple[int, int]], Dict[Tuple[int, int], float]]) -> bool:
        """這一季 MoviePilot 已經在處理（還訂閱著，或剛送去下載、還沒入庫）就記一筆、回傳 True：不重複訂閱。"""
        subscribed, sent = handed
        if key in subscribed:
            r.existing += 1
            r.details.append(f"{text}；MoviePilot 裡已經有這一季的訂閱，它會在定時搜尋時處理")
            return True
        at = sent.get(key)
        if at and sent_covers(at, episodes, missing):
            r.sent += 1
            r.details.append(f"{text}；MoviePilot 在 {time.strftime('%m-%d %H:%M', time.localtime(at))} 已經找到資源、"
                             "送去下載，等下載完入庫，不重複訂閱")
            return True
        return False

    def cancel(self, what: str) -> bool:
        """按了停止（what 是 scrape 或 fill）：刮削送出去的做完、沒送的不送；補全缺集做完手上這一季就停。
        沒在跑回傳 False。"""
        running = {"scrape": self.result.running, "fill": self.fill_result.running,
                   "unsubscribe": self.unsubscribe_result.running}.get(what)
        if what not in self._cancel or not running:
            return False
        self._cancel[what].set()
        return True

    # ---------------- 取消訂閱 ----------------

    def subscriptions(self) -> List[dict]:
        """MoviePilot 裡這個帳號看得到的訂閱（管理員看得到全部）：id、name、year、type（电视剧／电影）、season。"""
        res = self._request("GET", SUBSCRIBE_API, timeout=60)
        items = res.get("data") if isinstance(res, dict) else res
        return [{"id": int(s["id"]), "name": str(s.get("name") or ""), "year": s.get("year"), "type": str(s.get("type") or ""),
                 "season": s.get("season"), "tmdbid": _sub_tmdbid(s)}
                for s in items or [] if isinstance(s, dict) and str(s.get("id") or "").isdecimal()]

    def known_subscriptions(self) -> Optional[List[dict]]:
        """給清單標「已訂閱」用的訂閱清單，記 SUBS_CACHE_SECONDS 秒；沒設定 MoviePilot、連不上、被拒絕時回傳 None
        （清單照常顯示，只是不標）。補全、取消訂閱做完時會清掉，下次重新讀。"""
        if not self.enabled:
            return None
        cached = self._subs_cache
        if cached and time.time() - cached[0] < SUBS_CACHE_SECONDS:
            return cached[1]
        try:
            subs: Optional[List[dict]] = self.subscriptions()
        except MoviePilotError as exc:
            log.info("讀不到 MoviePilot 的訂閱，清單先不標已訂閱：%s", exc)
            subs = None
        self._subs_cache = (time.time(), subs)
        return subs

    def subscribed_seasons(self) -> Optional[Set[Tuple[int, int]]]:
        """MoviePilot 裡已經訂閱的（tmdbid, 季）；讀不到是 None。"""
        subs = self.known_subscriptions()
        if subs is None:
            return None
        return {(int(s["tmdbid"]), int(s["season"])) for s in subs
                if str(s.get("tmdbid") or "").isdecimal() and str(s.get("season") or "").isdecimal()}

    def sent_seasons(self) -> Dict[Tuple[int, int], float]:
        """MoviePilot 最近（SENT_GRACE_SECONDS 以內）完成的劇集訂閱：{(tmdbid, 季): 完成的時間}。它找到資源、交給下載器
        就算完成、移到訂閱歷史，集還在下載、還沒入庫；清單把這些季標「已送下載」，補全不重複訂閱。
        記 SUBS_CACHE_SECONDS 秒；沒設定 MoviePilot、讀不到（舊版沒有這個 API）是空的。"""
        if not self.enabled:
            return {}
        cached = self._sent_cache
        if cached and time.time() - cached[0] < SUBS_CACHE_SECONDS:
            return cached[1]
        sent: Dict[Tuple[int, int], float] = {}
        oldest = time.time() - SENT_GRACE_SECONDS
        try:
            for page in range(1, HISTORY_MAX_PAGES + 1):
                res = self._request("GET", SUBSCRIBE_HISTORY_API + TV_TYPE, timeout=60, query={"page": page, "count": HISTORY_PAGE})
                rows = res.get("data") if isinstance(res, dict) else res
                rows = [s for s in rows or [] if isinstance(s, dict)]
                times = [_history_time(s) for s in rows]
                for s, at in zip(rows, times):
                    tmdbid, season = str(_sub_tmdbid(s) or ""), str(s.get("season") or "")
                    if at >= oldest and tmdbid.isdecimal() and season.isdecimal():
                        key = (int(tmdbid), int(season))
                        sent[key] = max(at, sent.get(key, 0.0))
                if len(rows) < HISTORY_PAGE or min(times) < oldest:
                    break  # 新的在前面：讀到比期限舊的，後面的都更舊
        except MoviePilotError as exc:
            log.info("讀不到 MoviePilot 的訂閱歷史，清單先不標已送下載：%s", exc)
        self._sent_cache = (time.time(), sent)
        return sent

    def subscription_counts(self) -> dict:
        subs = self.subscriptions()
        tv = sum(1 for s in subs if s["type"] == TV_TYPE)
        return {"total": len(subs), "tv": tv, "other": len(subs) - tv}

    def unsubscribe_all(self) -> UnsubscribeResult:
        """把 MoviePilot 裡的訂閱一個一個刪掉（DELETE /api/v1/subscribe/{id}）。只刪訂閱，下載好、整理好的檔案不動。
        和補全缺集不同時跑（共用一把鎖）。清單可能分頁，所以刪完一輪再列一次，直到沒有還沒試過的。"""
        if not self._fill_lock.acquire(blocking=False):
            return self.unsubscribe_result
        self.unsubscribe_result = r = UnsubscribeResult(started=time.time(), running=True)
        self._cancel["unsubscribe"].clear()
        tried: Set[int] = set()
        try:
            if not self.enabled:
                raise MoviePilotError("還沒設定 MoviePilot")
            while True:
                todo = [s for s in self.subscriptions() if s["id"] not in tried]
                r.total = len(tried) + len(todo)
                if not todo:
                    break
                for s in todo:
                    if self._stop.is_set() or self._cancel["unsubscribe"].is_set():
                        r.stopped = not self._stop.is_set()
                        return r
                    tried.add(s["id"])
                    label = s["name"] + (f" S{int(s['season']):02d}" if str(s.get("season") or "").isdecimal() else "")
                    r.current = label
                    try:
                        self._request("DELETE", f"{SUBSCRIBE_API}{s['id']}", timeout=60)
                        r.done += 1
                    except MoviePilotError as exc:
                        if exc.status == 404:  # 已經不在了（別的地方刪掉）
                            r.done += 1
                            continue
                        if exc.kind or exc.status in (401, 403):
                            raise  # 連不上、被拒絕：後面的也不會成功
                        r.failed += 1
                        if len(r.errors) < MAX_UNSUB_ERRORS:
                            r.errors.append(f"{label}：{exc}")
            log.info("取消 MoviePilot 的訂閱：刪了 %s 個，失敗 %s 個", r.done, r.failed)
        except MoviePilotError as exc:
            r.errors.append(str(exc))
            log.error("取消訂閱中止：%s", exc)
        finally:
            r.running = False
            r.current = ""
            r.finished = time.time()
            self._subs_cache = self._sent_cache = None
            self._fill_lock.release()
        return r

    def unsubscribe_all_in_background(self) -> bool:
        if self._fill_lock.locked():
            return False
        self.workers.start(self.unsubscribe_all)
        return True

    def fill_excluded(self) -> Dict[str, str]:
        """標了「不補」的劇：tmdbid → 劇名（照 tmdbid 記，重新掃描、改資料夾名都還在）。"""
        if self.db is None:
            return {}
        try:
            value = json.loads(self.db.get_meta(FILL_EXCLUDED_KEY) or "{}")
        except ValueError:
            return {}
        return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}

    def set_fill_excluded(self, tmdbid: int, name: str, on: bool) -> Dict[str, str]:
        """標記或取消「不補」：補全缺集（手動、全部、全量同步後自動）都跳過這部劇。"""
        if self.db is None:
            raise MoviePilotError("沒有資料庫，記不住要跳過的劇")
        with self._fill_excluded_lock:
            excluded = self.fill_excluded()
            if on:
                excluded[str(tmdbid)] = name
            else:
                excluded.pop(str(tmdbid), None)
            self.db.set_meta(FILL_EXCLUDED_KEY, json.dumps(excluded, ensure_ascii=False))
        return excluded

    def fill_in_background(self, series: List[dict], source: str, check: bool = False, force: bool = False) -> bool:
        if self._fill_lock.locked():
            return False
        self.workers.start(self.fill, series, source, check, force)
        return True


def _sub_tmdbid(sub: dict):
    """訂閱（或訂閱歷史）的 tmdbid。新版 MoviePilot 不回 tmdbid，改成 media_source="themoviedb" 加 media_id。"""
    if sub.get("tmdbid"):
        return sub["tmdbid"]
    return sub.get("media_id") if sub.get("media_source") == "themoviedb" else None


def _history_time(row: dict) -> float:
    """訂閱歷史的 date（"2026-10-02 02:07:08"，MoviePilot 那台機器的本地時間）→ 時間戳；看不懂的當成很久以前。"""
    try:
        return datetime.fromisoformat(str(row.get("date") or "")).timestamp()
    except ValueError:
        return 0.0


def sent_covers(at: float, episodes: Dict[int, str], missing: List[int]) -> bool:
    """MoviePilot 在 at 完成的訂閱，涵蓋了現在缺的這幾集嗎：那之後才播出的集它當時還抓不到，要另外補。"""
    day = date.fromtimestamp(at).isoformat()
    return all((episodes.get(e) or "") <= day for e in missing)


def season_missing(episodes: Dict[int, str], have: Set[int], today: Optional[str] = None) -> Tuple[List[int], List[int], int]:
    """對照 TMDB 這一季的集（集號 → 播出日期）和媒體庫裡有的集號：(缺的集, 不確定播了沒的集, 已播出幾集)。

    有播出日期的看日期。沒有日期的常是還沒播的佔位集，只有集號不超過媒體庫裡最後一集的才確定播過
    （都有第 10 集了，第 3 集一定播過）；比最後一集後面又沒有日期的不確定，不算缺。"""
    today = today or date.today().isoformat()
    last = max(have) if have else 0
    aired = {e for e, d in episodes.items() if (d <= today if d else e <= last)}
    undated = sorted(e for e, d in episodes.items() if not d and e > last)
    return sorted(aired - have), undated, len(aired)


def _load_episodes(raw: str) -> Optional[Dict[int, str]]:
    """tmdb_seasons.episodes（JSON）→ {集號: 播出日期}；空的 {} 是 TMDB 沒有這一季；壞掉的回傳 None（當成沒查過）。"""
    try:
        return {int(k): str(v or "") for k, v in json.loads(raw or "{}").items()}
    except (ValueError, AttributeError, TypeError):
        return None


def ep_ranges(nums: List[int], limit: int = 6) -> str:
    """連號的集壓成範圍：E27–E61、E86–E94…"""
    runs: List[List[int]] = []
    for n in sorted(nums):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    text = "、".join(f"E{a:02d}" if a == b else f"E{a:02d}–E{b:02d}" for a, b in runs[:limit])
    return text + ("…" if len(runs) > limit else "")


# 一部劇在補全缺集裡的狀態（互不重疊，清單的分頁照這個分）
MISSING, PENDING, MISMATCH, UNCHECKED, NO_TMDB, COMPLETE, EXCLUDED = (
    "missing", "pending", "mismatch", "unchecked", "notmdb", "complete", "excluded")
STATE_ORDER = {MISSING: 0, PENDING: 1, MISMATCH: 2, UNCHECKED: 3, NO_TMDB: 4, COMPLETE: 5, EXCLUDED: 6}
VIEWS = {"missing": {MISSING}, "pending": {PENDING}, "mismatch": {MISMATCH}, "unchecked": {UNCHECKED, NO_TMDB},
         "excluded": {EXCLUDED}}


def library_series(
    db: Database, query: str = "", gaps_only: bool = False, limit: int = 0, offset: int = 0,
    year: Optional[int] = None, view: str = "", excluded: Optional[Set[str]] = None,
    subscribed: Optional[Set[Tuple[int, int]]] = None, stats: Optional[dict] = None, library: Optional[int] = None,
    sent: Optional[Dict[Tuple[int, int], float]] = None,
) -> Tuple[List[dict], int]:
    """媒體庫裡的劇：名稱、年份、tmdbid、每一季有幾集、集號的空洞（有第 2、4 集沒有第 3 集），
    以及對照 TMDB 缺哪幾集（檢查缺集、補全缺集時記下的 tmdb_seasons；還沒對照過的季是 None）。

    每一部有一個 state（互不重疊，照「接下來要做什麼」分）：missing = 對照過、缺集、還有季沒交給 MoviePilot（要補）；
    pending = 缺集的季 MoviePilot 都在處理了（等它下載、入庫）；mismatch = 有季在 TMDB 上不存在（季號對不上，要先整理）；
    unchecked = 還有季沒對照過 TMDB（要檢查）；notmdb = 沒有 tmdbid（要先刮削）；complete = 對照過、齊全；
    excluded = 標了「不補」（excluded：這些 tmdbid 字串）。每一季的 absent 是 TMDB 上沒有這一季。
    MoviePilot 在處理的季有兩種：subscribed 參數是它裡面還訂閱著的（tmdbid, 季）；sent 參數是它最近找到資源、送去下載、
    把訂閱記成完成的（sent_seasons：{(tmdbid, 季): 完成的時間}），集還沒入庫，那之後才播出的集不算在內。
    都沒給（讀不到）時缺集的都算 missing。to_fill 是還要補的缺集季數。

    回傳 (清單, 符合條件的總數)。篩選：query 搜尋劇名、year 只列那一年的、library 只列那個媒體庫（項目 id）的、
    gaps_only 只列集號有空洞的；view 再從篩選出來的裡面只列一種狀態：missing、pending、mismatch、unchecked（含 notmdb）、excluded，
    空的是全部。limit、offset 分頁。照 state 排，要補的在最前面。特別篇（第 0 季）不算。
    stats 給了就填上篩選出來的（不看 view）各種狀態幾部和 total：網頁的分頁數字、「補全這 N 部」都照篩選算。
    """
    episodes: Dict[int, Dict[int, Set[int]]] = {}
    for r in db.query(
        "SELECT series_id, parent_index_number AS s, index_number AS e FROM items "
        "WHERE type='Episode' AND series_id IS NOT NULL AND parent_index_number IS NOT NULL AND index_number IS NOT NULL"
    ):
        episodes.setdefault(r["series_id"], {}).setdefault(int(r["s"]), set()).add(int(r["e"]))
    tmdb: Dict[Tuple[int, int], Optional[Dict[int, str]]] = {
        (r["tmdbid"], r["season"]): _load_episodes(r["episodes"]) for r in db.query("SELECT tmdbid, season, episodes FROM tmdb_seasons")}
    today = date.today().isoformat()
    excluded = excluded or set()
    counts = {MISSING: 0, PENDING: 0, MISMATCH: 0, UNCHECKED: 0, NO_TMDB: 0, COMPLETE: 0, EXCLUDED: 0, "total": 0}
    sent = sent or {}
    needle = simplified(query.strip()).lower()
    needle_py = pinyin_full(query) if cjk_count(query) >= 2 else ""
    out: List[dict] = []
    for r in db.query(
        "SELECT i.id, i.name, i.year, i.original_title, i.provider_ids, i.search_text, i.library_id, l.name AS library "
        "FROM items i LEFT JOIN items l ON l.id=i.library_id WHERE i.type='Series' ORDER BY i.sort_name"
    ):
        name = r["name"] or ""
        hay = f"{name} {r['original_title'] or ''} {r['year'] or ''} {r['search_text'] or ''}".lower()
        if needle and needle not in hay and not (needle_py and needle_py in hay):
            continue  # 片名、原名、年份、拼音、首字母都認
        if (year and r["year"] != year) or (library and r["library_id"] != library):
            continue
        providers = json.loads(r["provider_ids"]) if r["provider_ids"] else {}
        tmdbid = str(providers.get("Tmdb") or "")
        has_id = tmdbid.isdecimal()
        seasons = []
        for season, eps in sorted(episodes.get(r["id"], {}).items()):
            if season == 0:
                continue
            gaps = sorted(set(range(min(eps), max(eps) + 1)) - eps)
            known = tmdb.get((int(tmdbid), season)) if has_id else None
            missing, _, aired = season_missing(known, eps, today) if known else (None, None, None)
            key = (int(tmdbid), season) if has_id else None
            sent_at = sent.get(key) if missing and key else None
            seasons.append({
                "season": season, "count": len(eps), "first": min(eps), "last": max(eps), "gaps": gaps, "tmdb": aired,
                "missing": missing, "absent": known == {},
                "subscribed": key in subscribed if missing and subscribed is not None else None,
                "sent": sent_at if sent_at and sent_covers(sent_at, known or {}, missing) else None})
        gap_count = sum(len(x["gaps"]) for x in seasons)
        missing_count = sum(len(x["missing"] or []) for x in seasons)
        absent = sum(1 for x in seasons if x["absent"]) if has_id else 0
        unchecked = sum(1 for x in seasons if x["missing"] is None and not x["absent"]) if has_id else 0
        to_fill = sum(1 for x in seasons if x["missing"] and not x["subscribed"] and not x["sent"])
        state = (NO_TMDB if not has_id else EXCLUDED if tmdbid in excluded else MISSING if to_fill
                 else PENDING if missing_count else MISMATCH if absent else UNCHECKED if unchecked else COMPLETE)
        counts[state] += 1
        counts["total"] += 1
        if view in VIEWS and state not in VIEWS[view]:
            continue
        if gaps_only and not gap_count:
            continue
        out.append({
            "id": r["id"], "name": name, "year": r["year"], "library": r["library"],
            "tmdbid": int(tmdbid) if has_id else None, "seasons": seasons, "gaps": gap_count,
            "missing": missing_count, "unchecked": unchecked, "absent": absent, "state": state, "to_fill": to_fill,
        })
    out.sort(key=lambda x: (STATE_ORDER[x["state"]], -x["missing"], -x["gaps"], x["name"].lower()))
    if stats is not None:
        stats.update(counts)
    total = len(out)
    out = out[offset:offset + limit] if limit else out[offset:]
    return out, total
