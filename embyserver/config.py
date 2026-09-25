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
    interval: int = 0  # 定時同步間隔（分鐘），0 表示只手動同步
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
class ServerConfig:
    name: str = "Emby Server"
    host: str = "0.0.0.0"
    port: int = 8096
    data_dir: str = "./data"
    public_users: bool = True


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    users: List[UserConfig] = field(default_factory=list)
    libraries: List[LibraryConfig] = field(default_factory=list)
    redirect: RedirectConfig = field(default_factory=RedirectConfig)
    api_keys: List[str] = field(default_factory=list)
    p115: P115Config = field(default_factory=P115Config)

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
    )


def _build_p115(raw: dict) -> P115Config:
    raw = dict(raw)
    sraw = dict(raw.pop("strm", None) or {})
    tasks = [StrmTask(remote=t["remote"], local=t["local"]) for t in sraw.pop("tasks", None) or []]
    return P115Config(strm=P115StrmConfig(tasks=tasks, **sraw), **raw)


def load_config(path: Optional[str] = None) -> Config:
    path = path or os.environ.get("EMBYSERVER_CONFIG", "config.yaml")
    p = Path(path)
    if not p.exists():
        # 設定檔可有可無：沒有時用預設值啟動，其餘在網頁上設定
        log.info("沒有設定檔 %s，使用預設值；請到 http://<主機>:<埠>/web 完成設定", p)
        return _build({})
    with p.open(encoding="utf-8") as f:
        return _build(yaml.safe_load(f))


def config_from_dict(raw: dict) -> Config:
    return _build(raw)
