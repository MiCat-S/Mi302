"""設定檔載入。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


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
    )


def load_config(path: Optional[str] = None) -> Config:
    path = path or os.environ.get("EMBYSERVER_CONFIG", "config.yaml")
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到設定檔：{p}")
    with p.open(encoding="utf-8") as f:
        return _build(yaml.safe_load(f))


def config_from_dict(raw: dict) -> Config:
    return _build(raw)
