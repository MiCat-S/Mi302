"""刮削交給 MoviePilot：把需要刮削的 strm 路徑送到 MoviePilot 的刮削 API。

MoviePilot 的 POST /api/v1/media/scrape/local 會依路徑辨識影片、到 TMDB 等來源查資料，
在同一個資料夾寫入 nfo 與圖片；Mi302 之後重新掃描就讀得到。兩邊必須看得到同一批檔案，
路徑不同時用 path_mappings 轉換。

送出的單位：
- 電影：strm 檔本身（MoviePilot 會寫 nfo 和同資料夾的海報）。
- 劇集：整部劇還沒有 tvshow.nfo 時送劇集資料夾（一次處理劇、季、集）；
  已經刮削過的劇只送新的那幾集。
已經有 nfo 的項目不送，避免覆蓋 115 上帶下來或之前刮好的資料。

補全缺集：替媒體庫裡的劇、每一季向 MoviePilot 建一條訂閱（POST /api/v1/subscribe/）。
MoviePilot 會拿 TMDB 的集數對照媒體伺服器（也就是 Mi302）裡已有的集，只下載缺的；
已經齊全的直接拒絕，不會留下訂閱。建訂閱的 API 只接受帳號登入，不接受 API 令牌。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

import httpx

from .config import Config, MoviePilotConfig
from .db import Database
from .http_util import GuardedClient

log = logging.getLogger(__name__)

SCRAPE_API = "/api/v1/media/scrape/local"
LOGIN_API = "/api/v1/login/access-token"
SUBSCRIBE_API = "/api/v1/subscribe/"
VIDEO_EXTS = {
    ".strm", ".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv",
    ".webm", ".rmvb", ".mpg", ".mpeg", ".iso", ".3gp",
}


class MoviePilotError(Exception):
    pass


@dataclass
class ScrapeResult:
    source: str = ""  # sync / manual
    started: float = 0.0
    finished: float = 0.0
    running: bool = False
    total: int = 0
    done: int = 0
    failed: int = 0
    current: str = ""
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class FillResult:
    """補全缺集：替劇的每一季向 MoviePilot 建訂閱的結果。"""

    source: str = ""  # sync / manual
    started: float = 0.0
    finished: float = 0.0
    running: bool = False
    total: int = 0  # 送出的季數
    done: int = 0
    created: int = 0  # 建了訂閱（有缺集）
    complete: int = 0  # MoviePilot 說媒體庫已經齊全
    existing: int = 0  # 之前就訂閱過
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
    ):
        self.cfg = cfg
        self.config = config
        self.on_done = on_done
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

    def _post(self, path: str, body: dict, timeout: Optional[float] = None) -> dict:
        if not self.cfg.url:
            raise MoviePilotError("還沒設定 MoviePilot 網址")
        with self._client(timeout or self.cfg.timeout) as client:
            for attempt in range(2):
                headers, params = {}, {}
                if self._jwt:
                    headers["Authorization"] = f"Bearer {self._jwt}"
                elif self.cfg.api_token:
                    # 新版接受 X-API-KEY 標頭；token 查詢參數給接受 API 令牌的舊端點
                    headers["X-API-KEY"] = self.cfg.api_token
                    params["token"] = self.cfg.api_token
                resp = client.post(self.cfg.url.rstrip("/") + path, json=body, headers=headers, params=params)
                if resp.status_code in (401, 403) and attempt == 0 and self.cfg.username and self.cfg.password:
                    # 舊版 MoviePilot 的刮削 API 只認登入 token；或是之前的登入 token 過期了
                    self._jwt = self._login(client)
                    continue
                if resp.status_code in (401, 403):
                    hint = "API 令牌不正確" if self.cfg.api_token else "請填 API 令牌"
                    if self.cfg.api_token and not self.cfg.username:
                        hint += "；若 MoviePilot 版本較舊，請改填 MoviePilot 的帳號密碼"
                    raise MoviePilotError(f"MoviePilot 拒絕存取（HTTP {resp.status_code}）：{hint}")
                if resp.status_code == 404:
                    raise MoviePilotError(f"MoviePilot 沒有這個 API（{path}），請確認網址或升級 MoviePilot")
                if resp.status_code >= 400:
                    if resp.text.lstrip().startswith("<"):
                        raise MoviePilotError(f"MoviePilot 回應 HTTP {resp.status_code}，內容是網頁不是 API，請確認網址")
                    raise MoviePilotError(f"MoviePilot 回應 HTTP {resp.status_code}：{resp.text[:200]}")
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
        body = self._post(SCRAPE_API, item)
        ok = bool(body.get("success"))
        message = body.get("message") or ("完成" if ok else "失敗")
        if not ok and "不存在" in message:
            message += f"（MoviePilot 找不到 {mp_path}，請檢查路徑對應）"
        return ok, message

    # ---------------- 決定要送哪些路徑 ----------------

    def _library_of(self, path: Path) -> Tuple[Optional[str], Optional[Path]]:
        best: Tuple[Optional[str], Optional[Path]] = (None, None)
        for lib in self.config.libraries:
            for root in lib.paths:
                r = Path(root).expanduser()
                if (path == r or r in path.parents) and (best[1] is None or len(str(r)) > len(str(best[1]))):
                    best = (lib.type, r)
        return best

    def plan(self, paths: Iterable[str]) -> List[Tuple[Path, bool]]:
        """把影片（strm）路徑轉成要送去刮削的清單 [(路徑, 是否資料夾)]，略過已經有 nfo 的。"""
        out: List[Tuple[Path, bool]] = []
        seen = set()

        def add(p: Path, is_dir: bool):
            if p not in seen:
                seen.add(p)
                out.append((p, is_dir))

        for raw in paths:
            p = Path(raw)
            if p.suffix.lower() not in VIDEO_EXTS or p.with_suffix(".nfo").exists():
                continue
            ltype, root = self._library_of(p)
            if ltype == "tvshows" and root is not None:
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
                if root is not None and p.parent != root and (p.parent / "movie.nfo").exists():
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

    def scrape(self, paths: Iterable[str], source: str) -> ScrapeResult:
        if not self._lock.acquire(blocking=False):
            log.info("MoviePilot 刮削已在進行，略過")
            return self.result
        self.result = ScrapeResult(source=source, started=time.time(), running=True)
        scraped: List[str] = []
        try:
            items = self.plan(paths)
            self.result.total = len(items)
            log.info("送 %s 個項目給 MoviePilot 刮削", len(items))
            for path, is_dir in items:
                self.result.current = str(path)
                try:
                    ok, message = self.scrape_one(path, is_dir)
                except MoviePilotError as exc:
                    # 連線或認證錯誤，後面的也不會成功
                    self.result.failed += self.result.total - self.result.done - self.result.failed
                    self.result.errors.append(str(exc))
                    log.error("MoviePilot 刮削中止：%s", exc)
                    break
                if ok:
                    self.result.done += 1
                    scraped.append(str(path))
                else:
                    self.result.failed += 1
                    self.result.errors.append(f"{path.name}：{message}")
                    log.warning("MoviePilot 刮削失敗 %s：%s", path, message)
        finally:
            self.result.running = False
            self.result.current = ""
            self.result.finished = time.time()
            self._lock.release()
        if scraped and self.on_done:
            self.on_done(scraped)  # 只重新掃描刮削過的地方
        return self.result

    def scrape_in_background(self, paths: Optional[List[str]], source: str) -> bool:
        """paths 為 None 時送媒體庫裡所有還沒有 nfo 的影片。"""
        if self._lock.locked():
            return False

        def job():
            self.scrape(self.missing() if paths is None else paths, source)

        threading.Thread(target=job, daemon=True).start()
        return True

    # ---------------- 補全缺集 ----------------

    @property
    def can_subscribe(self) -> bool:
        """建訂閱的 API 只接受帳號登入。"""
        return self.enabled and bool(self.cfg.username and self.cfg.password)

    def subscribe(self, name: str, year: Optional[int], tmdbid: int, season: int) -> Tuple[str, str]:
        """替一季劇向 MoviePilot 建訂閱。

        回傳 (結果, MoviePilot 的訊息)：created = 建了訂閱、complete = 媒體庫已經齊全、
        existing = 之前就訂閱過、failed = 其他失敗。
        """
        body = {"name": name, "year": str(year) if year else None, "type": "电视剧", "tmdbid": tmdbid, "season": season}
        res = self._post(SUBSCRIBE_API, body, timeout=60)
        message = str(res.get("message") or "")
        if res.get("success"):
            return "created", message or "已建立訂閱"
        if "订阅已存在" in message or "訂閱已存在" in message:
            return "existing", message
        if "已存在" in message:  # 媒体库中已存在
            return "complete", message
        return "failed", message or "MoviePilot 沒有說明原因"

    def fill(self, series: List[dict], source: str) -> FillResult:
        """替這些劇（library_series 的格式）的每一季建訂閱，一季一季來。"""
        if not self._fill_lock.acquire(blocking=False):
            log.info("補全缺集已在進行，略過")
            return self.fill_result
        self.fill_result = r = FillResult(source=source, started=time.time(), running=True)
        try:
            if not self.can_subscribe:
                raise MoviePilotError("建訂閱的 API 只接受帳號登入，請在 MoviePilot 連線設定填帳號密碼")
            jobs: List[Tuple[dict, int]] = []
            for show in series:
                if not show.get("tmdbid"):
                    r.skipped += 1
                    r.details.append(f"{show['name']}：沒有 tmdbid，略過（先刮削）")
                    continue
                jobs += [(show, x["season"]) for x in show.get("seasons") or []]
            r.total = len(jobs)
            log.info("補全缺集：替 %s 季向 MoviePilot 建訂閱", len(jobs))
            for show, season in jobs:
                label = f"{show['name']} S{season:02d}"
                r.current = label
                try:
                    outcome, message = self.subscribe(show["name"], show.get("year"), int(show["tmdbid"]), season)
                except MoviePilotError as exc:
                    # 連線或認證錯誤，後面的也不會成功
                    r.failed += r.total - r.done
                    r.errors.append(str(exc))
                    log.error("補全缺集中止：%s", exc)
                    break
                r.done += 1
                setattr(r, outcome, getattr(r, outcome) + 1)
                r.details.append(f"{label}：{message}")
                if outcome == "failed":
                    r.errors.append(f"{label}：{message}")
                    log.warning("MoviePilot 不接受訂閱 %s：%s", label, message)
                else:
                    log.info("補全缺集 %s：%s", label, message)
        except MoviePilotError as exc:
            r.errors.append(str(exc))
        finally:
            r.running = False
            r.current = ""
            r.finished = time.time()
            self._fill_lock.release()
        return r

    def fill_in_background(self, series: List[dict], source: str) -> bool:
        if self._fill_lock.locked():
            return False
        threading.Thread(target=self.fill, args=(series, source), daemon=True).start()
        return True


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
    needle = query.strip().lower()
    out: List[dict] = []
    for r in db.query(
        "SELECT i.id, i.name, i.year, i.original_title, i.provider_ids, l.name AS library FROM items i "
        "LEFT JOIN items l ON l.id=i.library_id WHERE i.type='Series' ORDER BY i.sort_name"
    ):
        name = r["name"] or ""
        if needle and needle not in f"{name} {r['original_title'] or ''} {r['year'] or ''}".lower():
            continue
        providers = json.loads(r["provider_ids"]) if r["provider_ids"] else {}
        tmdbid = str(providers.get("Tmdb") or "")
        seasons = []
        for season, eps in sorted(episodes.get(r["id"], {}).items()):
            if season == 0:
                continue
            gaps = sorted(set(range(min(eps), max(eps) + 1)) - eps)
            seasons.append({"season": season, "count": len(eps), "gaps": gaps})
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
