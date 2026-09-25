"""網頁上可修改的設定：存在資料庫 meta，啟動時蓋過設定檔的同名項目。

各元件持有的是同一個 Config 物件裡的子物件（config.libraries、config.p115.strm、
config.redirect…），所以這裡一律就地修改，不替換物件，改完立即生效。
"""

from __future__ import annotations

import copy
import json
from dataclasses import fields
from typing import Any, Dict, List

from .config import Config, LibraryConfig, PathRule
from .db import Database

SETTINGS_META_KEY = "web_settings"

# 網頁可修改的欄位；port、host、data_dir 牽涉啟動方式，不開放在網頁改
SERVER_FIELDS = ("name", "public_users")
STRM_FIELDS = (
    "base_url", "include_name", "download_metadata", "delete_stale",
    "min_size_mb", "interval", "request_delay", "scan_after_sync",
)
REDIRECT_FIELDS = ("resolve_redirects", "resolve_timeout", "cache_ttl", "require_auth", "default_container")
P115_FIELDS = ("app", "open_app_id")


class SettingsError(ValueError):
    pass


def export_settings(config: Config) -> Dict[str, Any]:
    return {
        "server": {k: getattr(config.server, k) for k in SERVER_FIELDS},
        "libraries": [{"name": l.name, "type": l.type, "paths": list(l.paths)} for l in config.libraries],
        "p115": {
            **{k: getattr(config.p115, k) for k in P115_FIELDS},
            "strm": {k: getattr(config.p115.strm, k) for k in STRM_FIELDS},
        },
        "redirect": {
            **{k: getattr(config.redirect, k) for k in REDIRECT_FIELDS},
            "path_rules": [{"from": r.source, "to": r.target} for r in config.redirect.path_rules],
        },
    }


def _coerce(obj, name: str, value):
    """依 dataclass 欄位原本的型別轉換網頁送來的值。"""
    current = getattr(obj, name)
    try:
        if isinstance(current, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if isinstance(current, int) and not isinstance(current, bool):
            return int(float(value or 0))
        if isinstance(current, float):
            return float(value or 0)
        return str(value if value is not None else "").strip()
    except (TypeError, ValueError):
        raise SettingsError(f"{name} 的值不正確：{value}")


def _set_fields(obj, allowed, raw: dict) -> None:
    names = {f.name for f in fields(obj)}
    for key in allowed:
        if key in raw and key in names:
            setattr(obj, key, _coerce(obj, key, raw[key]))


def _libraries(raw: list) -> List[LibraryConfig]:
    out: List[LibraryConfig] = []
    seen = set()
    for lib in raw or []:
        name = str(lib.get("name") or "").strip()
        paths = [str(p).strip() for p in lib.get("paths") or [] if str(p).strip()]
        if not name:
            raise SettingsError("媒體庫名稱不可空白")
        if name in seen:
            raise SettingsError(f"媒體庫名稱重複：{name}")
        if not paths:
            raise SettingsError(f"媒體庫「{name}」至少要有一個資料夾")
        seen.add(name)
        ltype = "tvshows" if str(lib.get("type")) == "tvshows" else "movies"
        out.append(LibraryConfig(name=name, type=ltype, paths=paths))
    return out


def apply_settings(config: Config, raw: dict) -> None:
    """把網頁設定就地套用到 config；raw 可以只包含部分區塊。"""
    raw = raw or {}
    if "libraries" in raw:
        config.libraries[:] = _libraries(raw["libraries"])
    if "server" in raw:
        _set_fields(config.server, SERVER_FIELDS, raw["server"] or {})
    p115 = raw.get("p115") or {}
    _set_fields(config.p115, P115_FIELDS, p115)
    if "strm" in p115:
        _set_fields(config.p115.strm, STRM_FIELDS, p115["strm"] or {})
    redirect = raw.get("redirect") or {}
    _set_fields(config.redirect, REDIRECT_FIELDS, redirect)
    if "path_rules" in redirect:
        rules = []
        for r in redirect["path_rules"] or []:
            src, dst = str(r.get("from") or "").strip(), str(r.get("to") or "").strip()
            if src and dst:
                rules.append(PathRule(source=src, target=dst))
        config.redirect.path_rules[:] = rules


def load_saved(db: Database, config: Config) -> None:
    raw = db.get_meta(SETTINGS_META_KEY)
    if raw:
        apply_settings(config, json.loads(raw))


def save(db: Database, config: Config, raw: dict) -> None:
    """驗證並套用，成功後把完整的網頁設定存進資料庫。"""
    apply_settings(copy.deepcopy(config), raw)  # 先在副本上驗證，失敗時不留下改一半的設定
    apply_settings(config, raw)
    db.set_meta(SETTINGS_META_KEY, json.dumps(export_settings(config), ensure_ascii=False))
