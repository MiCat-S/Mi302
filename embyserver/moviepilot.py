"""刮削交給 MoviePilot：把需要刮削的 strm 路徑送到 MoviePilot 的刮削 API。

MoviePilot 的 POST /api/v1/media/scrape/local 會依路徑辨識影片、到 TMDB 等來源查資料，
在同一個資料夾寫入 nfo 與圖片；Mi302 之後重新掃描就讀得到。兩邊必須看得到同一批檔案，
路徑不同時用 path_mappings 轉換。

送出的單位：
- 電影：strm 檔本身（MoviePilot 會寫 nfo 和同資料夾的海報）。
- 劇集：整部劇還沒有 tvshow.nfo 時送劇集資料夾（一次處理劇、季、集）；
  已經刮削過的劇只送新的那幾集。
已經有 nfo 的項目不送，避免覆蓋 115 上帶下來或之前刮好的資料。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

import httpx

from .config import Config, MoviePilotConfig
from .http_util import GuardedClient

log = logging.getLogger(__name__)

SCRAPE_API = "/api/v1/media/scrape/local"
LOGIN_API = "/api/v1/login/access-token"
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


class MoviePilot:
    def __init__(
        self,
        cfg: MoviePilotConfig,
        config: Config,
        on_done: Optional[Callable[[], None]] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.cfg = cfg
        self.config = config
        self.on_done = on_done
        self.result = ScrapeResult()
        self._lock = threading.Lock()
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
                    raise MoviePilotError("MoviePilot 沒有刮削 API，請確認網址或升級 MoviePilot")
                if resp.status_code >= 400:
                    raise MoviePilotError(f"MoviePilot 回應 HTTP {resp.status_code}：{resp.text[:200]}")
                try:
                    return resp.json()
                except ValueError:
                    raise MoviePilotError("MoviePilot 回應不是 JSON，請確認網址是 MoviePilot")
        raise MoviePilotError("MoviePilot 登入失敗")

    # ---------------- 功能 ----------------

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
                else:
                    self.result.failed += 1
                    self.result.errors.append(f"{path.name}：{message}")
                    log.warning("MoviePilot 刮削失敗 %s：%s", path, message)
        finally:
            self.result.running = False
            self.result.current = ""
            self.result.finished = time.time()
            self._lock.release()
        if self.result.done and self.on_done:
            self.on_done()
        return self.result

    def scrape_in_background(self, paths: Optional[List[str]], source: str) -> bool:
        """paths 為 None 時送媒體庫裡所有還沒有 nfo 的影片。"""
        if self._lock.locked():
            return False

        def job():
            self.scrape(self.missing() if paths is None else paths, source)

        threading.Thread(target=job, daemon=True).start()
        return True
