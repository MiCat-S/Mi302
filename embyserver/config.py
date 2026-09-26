"""設定檔載入。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

log = logging.getLogger(__name__)


@dataclass
class LibraryConfig:
    name: str
    type: str  # movies | tvshows
    paths: List[str]


@dataclass
class UserConfig:
    name: str
    password: str = ""
    admin: bool = False


@dataclass
class PathRule:
    """strm 內容的前綴替換規則：命中 from 前綴時改寫為 to。"""

    source: str
    target: str


@dataclass
class RedirectConfig:
    path_rules: List[PathRule] = field(default_factory=list)
    # 302 前先跟隨一次重導向鏈，把最終網址直接交給播放器
    resolve_redirects: bool = False
    resolve_timeout: float = 10.0
    cache_ttl: int = 90
    # stream 請求常不帶 token，預設不要求認證
    require_auth: bool = False
    default_container: str = "mkv"


@dataclass
class StrmTask:
    remote: str  # 115 上的目錄路徑
    local: str  # 產生 strm 的本機資料夾


@dataclass
class P115StrmConfig:
    tasks: List[StrmTask] = field(default_factory=list)
    # strm 內容為 {base_url}/d/{pickcode}.mkv；留空時自動使用管理員開 /web/115 的網址
    base_url: str = ""
    include_name: bool = False  # 在網址後附上 ?/原檔名，方便人工辨識
    download_metadata: bool = True  # 一併下載 nfo、圖片、字幕
    delete_stale: bool = False  # 刪除 115 上已不存在的 strm
    min_size_mb: float = 0  # 小於此大小的影片不產生 strm（例如預告片）
    interval: int = 0  # 自動增量同步間隔（分鐘），0 表示不自動
    full_interval: int = 168  # 自動全量同步間隔（小時），預設每週一次查漏補缺；0 表示不自動
    request_delay: float = 0.2  # 每列一個目錄前的等待秒數，避免被 115 風控
    scan_after_sync: bool = True  # 同步完自動重新掃描媒體庫


@dataclass
class P115Config:
    # 可直接填 cookie，也可以之後在 /web/115 掃碼登入
    cookies: str = ""
    # 掃碼後綁定的 115 裝置類型；同類型的舊登入會被踢下線
    app: str = "alipaymini"
    timeout: float = 15.0
    # 進階：115 開放平台 AppID，只有自己在 open.115.com 申請到應用的人才需要
    open_app_id: str = ""
    strm: P115StrmConfig = field(default_factory=P115StrmConfig)


@dataclass
class MoviePilotConfig:
    """刮削交給 MoviePilot：Mi302 產生新的 strm 後，呼叫 MoviePilot 的刮削 API。"""

    url: str = ""  # MoviePilot 網址，例如 http://192.168.1.10:3000
    api_token: str = ""  # MoviePilot 設定 → 系統 → API 令牌
    # 舊版 MoviePilot 的刮削 API 只接受登入後的 token，這時改用帳號密碼
    username: str = ""
    password: str = ""
    # Mi302 看到的路徑 → MoviePilot 看到的路徑（兩邊容器掛載點不同時才需要）
    path_mappings: List[PathRule] = field(default_factory=list)
    scrape_after_sync: bool = True  # 同步產生新 strm 後自動送去刮削
    fill_after_full_sync: bool = False  # 全量同步後把所有有 tmdbid 的劇送給 MoviePilot 訂閱，補齊缺集
    timeout: float = 300  # MoviePilot 刮削是同步完成才回應，一部片可能要幾十秒
    concurrency: int = 3  # 同時送幾項給 MoviePilot 刮削（1–8）；太多可能被 TMDB 限速


@dataclass
class MediaInfoConfig:
    """用 ffprobe 探測 strm 指向的影片，產生 X-mediainfo.json（解析度、HDR、音軌、字幕軌、章節）。"""

    enabled: bool = False  # 預設關：每一項要向 115 取一次直鏈、讀幾 MB 檔頭
    after_sync: bool = True  # 同步產生新的 strm 後自動探測
    concurrency: int = 2  # 同時探測幾項（1–3）；115 同時最多 3 條連線
    interval: float = 1.0  # 每次向 115 取直鏈至少間隔幾秒（0.5–60）
    hourly_limit: int = 300  # 每小時最多向 115 取幾次直鏈（0 = 不限），首次整庫探測時保護帳號
    timeout: int = 300  # 每一項最多等幾秒
    ffprobe: str = "ffprobe"  # ffprobe 的路徑


@dataclass
class ServerConfig:
    name: str = "Emby Server"
    host: str = "0.0.0.0"
    port: int = 8096
    data_dir: str = "./data"
    public_users: bool = True
    log_level: str = "info"  # info = 一般；debug = 詳細（另外記錄每個播放器請求）


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    users: List[UserConfig] = field(default_factory=list)
    libraries: List[LibraryConfig] = field(default_factory=list)
    redirect: RedirectConfig = field(default_factory=RedirectConfig)
    api_keys: List[str] = field(default_factory=list)
    p115: P115Config = field(default_factory=P115Config)
    moviepilot: MoviePilotConfig = field(default_factory=MoviePilotConfig)
    mediainfo: MediaInfoConfig = field(default_factory=MediaInfoConfig)
    # 設定檔的位置與讀取時的修改時間；網頁儲存時寫回這個檔案，檔案被手動改過時重新讀取
    path: Optional[str] = field(default=None, repr=False, compare=False)
    file_mtime: float = field(default=0.0, repr=False, compare=False)

    @property
    def data_path(self) -> Path:
        return Path(self.server.data_dir).expanduser().resolve()


def _build(raw: dict) -> Config:
    raw = raw or {}
    server = ServerConfig(**(raw.get("server") or {}))
    users = [UserConfig(**u) for u in raw.get("users") or []]
    libraries = []
    for lib in raw.get("libraries") or []:
        paths = lib.get("paths") or ([lib["path"]] if lib.get("path") else [])
        libraries.append(
            LibraryConfig(name=lib["name"], type=lib.get("type", "movies"), paths=paths)
        )
    rraw = dict(raw.get("redirect") or {})
    rules = [
        PathRule(source=r["from"], target=r["to"]) for r in rraw.pop("path_rules", []) or []
    ]
    redirect = RedirectConfig(path_rules=rules, **rraw)
    return Config(
        server=server,
        users=users,
        libraries=libraries,
        redirect=redirect,
        api_keys=list(raw.get("api_keys") or []),
        p115=_build_p115(raw.get("p115") or {}),
        moviepilot=_build_moviepilot(raw.get("moviepilot") or {}),
        mediainfo=MediaInfoConfig(**(raw.get("mediainfo") or {})),
    )


def _build_moviepilot(raw: dict) -> MoviePilotConfig:
    raw = dict(raw)
    rules = [PathRule(source=r["from"], target=r["to"]) for r in raw.pop("path_mappings", None) or []]
    return MoviePilotConfig(path_mappings=rules, **raw)


def _build_p115(raw: dict) -> P115Config:
    raw = dict(raw)
    sraw = dict(raw.pop("strm", None) or {})
    tasks = [StrmTask(remote=t["remote"], local=t["local"]) for t in sraw.pop("tasks", None) or []]
    return P115Config(strm=P115StrmConfig(tasks=tasks, **sraw), **raw)


def load_config(path: Optional[str] = None) -> Config:
    path = path or os.environ.get("EMBYSERVER_CONFIG", "config.yaml")
    p = Path(path)
    if not p.exists():
        # 設定檔可有可無：沒有時用預設值啟動，網頁上第一次儲存設定時建立
        log.info("沒有設定檔 %s，使用預設值；請到 http://<主機>:<埠>/web 完成設定", p)
        config = _build({})
    else:
        config = read_file(p)
        config.file_mtime = p.stat().st_mtime
    config.path = str(p)
    return config


def read_file(path: Path) -> Config:
    try:
        with Path(path).open(encoding="utf-8") as f:
            return _build(yaml.safe_load(f))
    except (yaml.YAMLError, TypeError, KeyError) as exc:
        raise ValueError(f"設定檔 {path} 格式錯誤：{exc}") from exc


def config_from_dict(raw: dict) -> Config:
    return _build(raw)
