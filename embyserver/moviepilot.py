"""刮削交給 MoviePilot：把需要刮削的 strm 路徑送到 MoviePilot 的刮削 API。

MoviePilot 的 POST /api/v1/media/scrape/local 會依路徑辨識影片、到 TMDB 等來源查資料，
在同一個資料夾寫入 nfo 與圖片；Mi302 之後重新掃描就讀得到。兩邊必須看得到同一批檔案，
路徑不同時用 path_mappings 轉換。

送出的單位：
- 電影：strm 檔本身（MoviePilot 會寫 nfo 和同資料夾的海報）。
- 劇集：整部劇還沒有 tvshow.nfo 時送劇集資料夾（一次處理劇、季、集）；
  已經刮削過的劇只送新的那幾集。
已經有 nfo 的項目不送，避免覆蓋 115 上帶下來或之前刮好的資料；手動刮削時，有 nfo 卻沒有劇照的集也會再送。

加快速度：同時送好幾項（MoviePilot 的刮削 API 是同步的，一項要等 TMDB 搜尋、取資料、下載圖片）；
已經刮削過的劇，送單集時直接帶上 tmdbid，MoviePilot 不必再用檔名搜尋 TMDB。
MoviePilot 說完成之後再看一次有沒有真的寫出 nfo、劇照：認不出集數時它也會回報完成。

補全缺集：先向 MoviePilot 查 TMDB 上每一季的集和播出日期（GET /api/v1/tmdb/{tmdbid}/{季}），
對照媒體庫裡的集號，只替真的缺集的季建訂閱（POST /api/v1/subscribe/），再請它立刻搜尋
（POST /api/v1/subscribe/search/{訂閱 id}）。MoviePilot V3 建訂閱時不檢查媒體庫、不保證馬上搜尋，
所以這兩步 Mi302 自己做。建訂閱和搜尋的 API 只接受帳號登入，不接受 API 令牌。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

import httpx

from .config import Config, MoviePilotConfig
from .db import Database
from .filetypes import IMAGE_EXTS, LIBRARY_VIDEO_EXTS as VIDEO_EXTS
from .textutil import cjk_count, pinyin_full, simplified
from .http_util import GuardedClient

log = logging.getLogger(__name__)

SCRAPE_API = "/api/v1/media/scrape/local"
LOGIN_API = "/api/v1/login/access-token"
SUBSCRIBE_API = "/api/v1/subscribe/"
SUBSCRIBE_SEARCH_API = "/api/v1/subscribe/search/{sid}"
TMDB_EPISODES_API = "/api/v1/tmdb/{tmdbid}/{season}"
MAX_CONCURRENCY = 8
# 送過卻沒有劇照的集（TMDB 沒有這集的圖），這段時間內手動刮削不再重送
NO_IMAGE_RETRY_SECONDS = 30 * 86400


class MoviePilotError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status  # MoviePilot 回的 HTTP 狀態碼；不是 HTTP 錯誤時是 None


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
    return tmdb if tmdb.isdigit() else None


@dataclass
class FillResult:
    """補全缺集：替劇的每一季向 MoviePilot 建訂閱的結果。"""

    source: str = ""  # sync / manual
    started: float = 0.0
    finished: float = 0.0
    running: bool = False
    total: int = 0  # 檢查的季數
    done: int = 0
    created: int = 0  # 缺集，建了訂閱並請 MoviePilot 搜尋
    complete: int = 0  # 已播出的集都有，沒有建訂閱
    existing: int = 0  # 之前就訂閱過（也請它再搜一次）
    missing: int = 0  # 缺的集數合計
    skipped: int = 0  # 沒有 tmdbid 的劇，以劇計
    failed: int = 0
    current: str = ""
    details: List[str] = field(default_factory=list)  # 每一季的結果
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


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
        self._lock = threading.Lock()
        self._fill_lock = threading.Lock()
        self._transport = transport
        self._jwt: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.url and (self.cfg.api_token or (self.cfg.username and self.cfg.password)))

    # ---------------- HTTP ----------------

    def _client(self, timeout: float) -> httpx.Client:
        return GuardedClient(MoviePilotError, timeout=timeout, transport=self._transport)

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
        if not self.cfg.url:
            raise MoviePilotError("還沒設定 MoviePilot 網址")
        with self._client(timeout or self.cfg.timeout) as client:
            for attempt in range(2):
                headers, params = {}, dict(query or {})
                if self._jwt:
                    headers["Authorization"] = f"Bearer {self._jwt}"
                elif self.cfg.api_token:
                    # 新版接受 X-API-KEY 標頭；token 查詢參數給接受 API 令牌的舊端點
                    headers["X-API-KEY"] = self.cfg.api_token
                    params["token"] = self.cfg.api_token
                resp = client.request(method, self.cfg.url.rstrip("/") + path, json=body, headers=headers, params=params)
                if resp.status_code in (401, 403) and attempt == 0 and self.cfg.username and self.cfg.password:
                    # 舊版 MoviePilot 的刮削 API 只認登入 token；或是之前的登入 token 過期了
                    self._jwt = self._login(client)
                    continue
                if resp.status_code in (401, 403):
                    hint = "API 令牌不正確" if self.cfg.api_token else "請填 API 令牌"
                    if self.cfg.api_token and not self.cfg.username:
                        hint += "；若 MoviePilot 版本較舊，請改填 MoviePilot 的帳號密碼"
                    raise MoviePilotError(f"MoviePilot 拒絕存取（HTTP {resp.status_code}）：{hint}", resp.status_code)
                if resp.status_code == 404:
                    raise MoviePilotError(f"MoviePilot 沒有這個 API（{path}），請確認網址或升級 MoviePilot", 404)
                if resp.status_code >= 400:
                    if resp.text.lstrip().startswith("<"):
                        raise MoviePilotError(f"MoviePilot 回應 HTTP {resp.status_code}，內容是網頁不是 API，請確認網址", resp.status_code)
                    raise MoviePilotError(f"MoviePilot 回應 HTTP {resp.status_code}：{resp.text[:200]}", resp.status_code)
                try:
                    return resp.json()
                except ValueError:
                    raise MoviePilotError("MoviePilot 回應不是 JSON，請確認網址是 MoviePilot")
        raise MoviePilotError("MoviePilot 登入失敗")

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
        """劇集媒體庫裡一集所屬的劇集資料夾；不是劇集時回傳 None。"""
        ltype, root = self._library_of(path)
        if ltype != "tvshows" or root is None:
            return None
        parts = path.relative_to(root).parts
        return root / parts[0] if len(parts) >= 2 else None

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
                time.sleep(self.verify_wait)
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
        self.db.execute(
            "INSERT INTO mp_no_image(path, at) VALUES(?, ?) ON CONFLICT(path) DO UPDATE SET at=excluded.at",
            (str(path), int(now)),
        )

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
                parts = p.relative_to(root).parts
                if len(parts) >= 2:
                    series = root / parts[0]
                    if series in seen:
                        continue
                    if not (series / "tvshow.nfo").exists():
                        add(series, True)
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
        scraped: Dict[int, str] = {}
        abort = threading.Event()
        count = threading.Lock()

        def one(index: int, path: Path, is_dir: bool) -> None:
            if abort.is_set():
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

        threading.Thread(target=job, daemon=True).start()
        return True

    # ---------------- 補全缺集 ----------------

    @property
    def can_subscribe(self) -> bool:
        """建訂閱的 API 只接受帳號登入。"""
        return self.enabled and bool(self.cfg.username and self.cfg.password)

    def tmdb_episodes(self, tmdbid: int, season: int) -> Optional[Dict[int, str]]:
        """TMDB 上這一季的集號 → 播出日期（沒填是空字串），透過 MoviePilot 查；查不到時回傳 None。"""
        try:
            body = self._request("GET", TMDB_EPISODES_API.format(tmdbid=tmdbid, season=season), timeout=30)
        except MoviePilotError as exc:
            log.warning("向 MoviePilot 查 TMDB %s 第 %s 季的集數失敗：%s", tmdbid, season, exc)
            return None
        items = body.get("data") if isinstance(body, dict) else body  # V3 包在 data 裡，V2 直接是清單
        episodes = {
            int(e["episode_number"]): str(e.get("air_date") or "")[:10]
            for e in items or [] if isinstance(e, dict) and str(e.get("episode_number") or "").isdigit()
        }
        return episodes or None

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
        sid = int(data["id"]) if str(data.get("id") or "").isdigit() and int(data["id"]) else None
        if "订阅已存在" in message or "訂閱已存在" in message:  # V3 對已存在的訂閱也回 success
            return "existing", message, sid
        if res.get("success"):
            return "created", message or "已建立訂閱", sid
        if "已存在" in message:  # 舊版：媒体库中已存在
            return "complete", message, None
        return "failed", message or "MoviePilot 沒有說明原因", None

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

    def fill(self, series: List[dict], source: str) -> FillResult:
        """替這些劇（library_series 的格式）的每一季建訂閱，一季一季來。"""
        if not self._fill_lock.acquire(blocking=False):
            log.info("補全缺集已在進行，略過")
            return self.fill_result
        self.fill_result = r = FillResult(source=source, started=time.time(), running=True)
        try:
            if not self.can_subscribe:
                raise MoviePilotError("建訂閱的 API 只接受帳號登入，請在 MoviePilot 連線設定填帳號密碼")
            jobs: List[Tuple[dict, dict]] = []
            for show in series:
                if not show.get("tmdbid"):
                    r.skipped += 1
                    r.details.append(f"{show['name']}：沒有 tmdbid，略過（先刮削）")
                    continue
                jobs += [(show, x) for x in show.get("seasons") or []]
            r.total = len(jobs)
            log.info("補全缺集：檢查 %s 季", len(jobs))
            for show, info in jobs:
                label = f"{show['name']} S{info['season']:02d}"
                r.current = label
                try:
                    self._fill_season(r, show, info, label)
                except MoviePilotError as exc:
                    # 連線或認證錯誤，後面的也不會成功
                    r.failed += r.total - r.done
                    r.errors.append(str(exc))
                    log.error("補全缺集中止：%s", exc)
                    break
                r.done += 1
        except MoviePilotError as exc:
            r.errors.append(str(exc))
        finally:
            r.running = False
            r.current = ""
            r.finished = time.time()
            self._fill_lock.release()
        return r

    def _fill_season(self, r: FillResult, show: dict, info: dict, label: str) -> None:
        """一季：查 TMDB 已播出的集 → 缺集才建訂閱 → 請 MoviePilot 馬上搜尋。"""
        tmdbid, season = int(show["tmdbid"]), int(info["season"])
        episodes = self.tmdb_episodes(tmdbid, season)
        missing: List[int] = []
        unsure = ""
        if episodes is not None:
            last = info["last"] if info.get("count") else 0
            have = set(range(info["first"], last + 1)) - set(info.get("gaps") or []) if info.get("count") else set()
            today = date.today().isoformat()
            # 有播出日期的看日期。沒有日期的常是還沒播的佔位集，只有集號不超過媒體庫裡最後一集的才確定播過
            # （都有第 10 集了，第 3 集一定播過）；比最後一集後面又沒有日期的不確定，不算缺，只在結果裡說明
            aired = {e for e, d in episodes.items() if (d <= today if d else e <= last)}
            undated = sorted(e for e, d in episodes.items() if not d and e > last)
            if undated:
                unsure = f"另有 {len(undated)} 集（{ep_ranges(undated)}）TMDB 沒有播出日期，不確定播了沒，沒算進去"
            missing = sorted(aired - have)
            if not missing:
                r.complete += 1
                r.details.append(f"{label}：TMDB 已播出的 {len(aired)} 集都有，不建訂閱" + (f"；{unsure}" if unsure else ""))
                return
            r.missing += len(missing)
        lack = f"缺 {len(missing)} 集（{ep_ranges(missing)}）" if missing else "查不到 TMDB 集數，交給 MoviePilot 判斷"
        outcome, message, sid = self.subscribe(show["name"], show.get("year"), tmdbid, season)
        setattr(r, outcome, getattr(r, outcome) + 1)
        if outcome == "failed":
            r.details.append(f"{label}：{lack}；{message}")
            r.errors.append(f"{label}：{message}")
            log.warning("MoviePilot 不接受訂閱 %s：%s", label, message)
            return
        note = message
        if sid and outcome in ("created", "existing"):
            try:
                note += "；" + self.search_subscription(sid)
            except MoviePilotError as exc:
                note += f"；沒有安排到搜尋（{exc}），MoviePilot 會在定時搜尋時處理"
                log.warning("請 MoviePilot 搜尋訂閱 %s 失敗：%s", sid, exc)
        if unsure:
            note += f"；{unsure}"
        r.details.append(f"{label}：{lack}；{note}")
        log.info("補全缺集 %s：%s；%s", label, lack, note)

    def fill_in_background(self, series: List[dict], source: str) -> bool:
        if self._fill_lock.locked():
            return False
        threading.Thread(target=self.fill, args=(series, source), daemon=True).start()
        return True


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


def library_series(
    db: Database, query: str = "", gaps_only: bool = False, limit: int = 0, offset: int = 0
) -> Tuple[List[dict], int]:
    """媒體庫裡的劇：名稱、年份、tmdbid、每一季有幾集、集號的空洞（有第 2、4 集沒有第 3 集）。

    回傳 (清單, 符合條件的總數)；集號有空洞的排前面，limit、offset 分頁。特別篇（第 0 季）不算。
    空洞只是提示：最後幾集沒下到、整季都沒有的情況這裡看不出來，交給 MoviePilot 對照 TMDB。
    """
    episodes: Dict[int, Dict[int, Set[int]]] = {}
    for r in db.query(
        "SELECT series_id, parent_index_number AS s, index_number AS e FROM items "
        "WHERE type='Episode' AND series_id IS NOT NULL AND parent_index_number IS NOT NULL AND index_number IS NOT NULL"
    ):
        episodes.setdefault(r["series_id"], {}).setdefault(int(r["s"]), set()).add(int(r["e"]))
    needle = simplified(query.strip()).lower()
    needle_py = pinyin_full(query) if cjk_count(query) >= 2 else ""
    out: List[dict] = []
    for r in db.query(
        "SELECT i.id, i.name, i.year, i.original_title, i.provider_ids, i.search_text, l.name AS library FROM items i "
        "LEFT JOIN items l ON l.id=i.library_id WHERE i.type='Series' ORDER BY i.sort_name"
    ):
        name = r["name"] or ""
        hay = f"{name} {r['original_title'] or ''} {r['year'] or ''} {r['search_text'] or ''}".lower()
        if needle and needle not in hay and not (needle_py and needle_py in hay):
            continue  # 片名、原名、年份、拼音、首字母都認
        providers = json.loads(r["provider_ids"]) if r["provider_ids"] else {}
        tmdbid = str(providers.get("Tmdb") or "")
        seasons = []
        for season, eps in sorted(episodes.get(r["id"], {}).items()):
            if season == 0:
                continue
            gaps = sorted(set(range(min(eps), max(eps) + 1)) - eps)
            seasons.append({"season": season, "count": len(eps), "first": min(eps), "last": max(eps), "gaps": gaps})
        gap_count = sum(len(x["gaps"]) for x in seasons)
        if gaps_only and not gap_count:
            continue
        out.append({
            "id": r["id"], "name": name, "year": r["year"], "library": r["library"],
            "tmdbid": int(tmdbid) if tmdbid.isdigit() else None, "seasons": seasons, "gaps": gap_count,
        })
    out.sort(key=lambda x: (-x["gaps"], x["name"].lower()))
    total = len(out)
    out = out[offset:offset + limit] if limit else out[offset:]
    return out, total
