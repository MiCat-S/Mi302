"""網頁上可修改的設定，與設定檔 config.yaml 保持一致。

- 網頁儲存時：先在副本上驗證，寫進設定檔，再套用到執行中的設定。
- 設定檔被手動改過（修改時間變了）：網頁讀取設定時重新讀檔並套用。
- 舊版把網頁設定存在資料庫 meta，啟動時搬進設定檔。

各元件持有的是同一個 Config 物件裡的子物件（config.libraries、config.p115.strm、
config.redirect…），所以這裡一律就地修改，不替換物件，改完立即生效。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from dataclasses import fields
from pathlib import Path
from typing import Any, Dict, List

from . import config_file, logs
from .config import Config, LibraryConfig, PathRule, StrmTask, read_file
from .db import Database

log = logging.getLogger(__name__)

# 舊版存放網頁設定、同步任務的位置
SETTINGS_META_KEY = "web_settings"
TASKS_META_KEY = "p115_strm_tasks"

# 網頁可修改的欄位；port、host、data_dir 牽涉啟動方式，不開放在網頁改
SERVER_FIELDS = ("name", "public_users", "log_level", "backup_keep", "chinese_people", "chinese_genres", "intro_skip")
STRM_FIELDS = (
    "base_url", "include_name", "download_metadata", "delete_stale",
    "min_size_mb", "interval", "full_interval", "request_delay", "scan_after_sync",
)
MOVIEPILOT_FIELDS = (
    "url", "api_token", "username", "password", "scrape_after_sync", "fill_after_full_sync", "timeout", "concurrency",
)
MEDIAINFO_FIELDS = (
    "enabled", "after_sync", "on_demand", "concurrency", "interval", "hourly_limit", "timeout", "ffprobe",
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
            "strm": {
                **{k: getattr(config.p115.strm, k) for k in STRM_FIELDS},
                "tasks": [{"remote": t.remote, "local": t.local} for t in config.p115.strm.tasks],
            },
        },
        "moviepilot": {
            **{k: getattr(config.moviepilot, k) for k in MOVIEPILOT_FIELDS},
            "path_mappings": [{"from": r.source, "to": r.target} for r in config.moviepilot.path_mappings],
        },
        "mediainfo": {k: getattr(config.mediainfo, k) for k in MEDIAINFO_FIELDS},
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
        config.server.log_level = "debug" if str(config.server.log_level).lower() == "debug" else "info"
        config.server.backup_keep = max(0, min(config.server.backup_keep, 90))
    p115 = raw.get("p115") or {}
    _set_fields(config.p115, P115_FIELDS, p115)
    if "strm" in p115:
        _set_fields(config.p115.strm, STRM_FIELDS, p115["strm"] or {})
        if "tasks" in (p115["strm"] or {}):
            config.p115.strm.tasks[:] = _tasks(p115["strm"]["tasks"])
    redirect = raw.get("redirect") or {}
    _set_fields(config.redirect, REDIRECT_FIELDS, redirect)
    if "path_rules" in redirect:
        config.redirect.path_rules[:] = _rules(redirect["path_rules"])
    mp = raw.get("moviepilot") or {}
    _set_fields(config.moviepilot, MOVIEPILOT_FIELDS, mp)
    config.moviepilot.url = config.moviepilot.url.rstrip("/")
    config.moviepilot.concurrency = max(1, min(config.moviepilot.concurrency, 8))
    if config.moviepilot.url and not config.moviepilot.url.startswith(("http://", "https://")):
        raise SettingsError("MoviePilot 網址要以 http:// 或 https:// 開頭")
    if "path_mappings" in mp:
        config.moviepilot.path_mappings[:] = _rules(mp["path_mappings"])
    mi = config.mediainfo
    _set_fields(mi, MEDIAINFO_FIELDS, raw.get("mediainfo") or {})
    mi.concurrency = max(1, min(mi.concurrency, 3))  # 115 同時最多 3 條連線
    mi.interval = max(0.5, min(mi.interval, 60.0))  # 最多每秒 2 次，115 的 WAF 很敏感
    mi.timeout = max(10, min(mi.timeout, 3600))
    mi.hourly_limit = max(0, min(mi.hourly_limit, 100000))
    mi.ffprobe = mi.ffprobe or "ffprobe"


def _tasks(raw) -> List[StrmTask]:
    tasks = []
    seen: List[Path] = []
    for t in raw or []:
        remote, local = str(t.get("remote") or "").strip(), str(t.get("local") or "").strip()
        if not (remote and local):
            raise SettingsError("115 目錄和本機資料夾都要填")
        folder = Path(local).expanduser()
        if not folder.is_absolute():
            # 相對路徑會跟著服務的工作目錄跑，strm 會產生到（並從）不知道哪裡刪
            raise SettingsError(f"本機資料夾要填完整路徑（例如 /volume1/media/電影）：{local}")
        folder = Path(os.path.normpath(folder))
        for other in seen:
            if folder == other or folder in other.parents or other in folder.parents:
                raise SettingsError(f"兩個任務的本機資料夾不能相同或互相包含：{other} 和 {folder}（刪除多餘 strm 時會互刪）")
        seen.append(folder)
        tasks.append(StrmTask(remote=remote if remote.startswith("/") else "/" + remote, local=local))
    return tasks


def _rules(raw) -> List[PathRule]:
    rules = []
    for r in raw or []:
        src, dst = str(r.get("from") or "").strip(), str(r.get("to") or "").strip()
        if src and dst:
            rules.append(PathRule(source=src, target=dst))
    return rules


def _write(config: Config) -> None:
    if not config.path:
        return
    try:
        config.file_mtime = config_file.write(config, config.path)
    except OSError as exc:
        raise SettingsError(f"無法寫入設定檔 {config.path}：{exc}") from exc


def load_saved(db: Database, config: Config) -> None:
    """啟動時：把舊版存在資料庫的網頁設定、同步任務搬進設定檔；沒有設定檔時產生一個。"""
    raw = db.get_meta(SETTINGS_META_KEY)
    tasks = db.get_meta(TASKS_META_KEY)
    if raw:
        apply_settings(config, json.loads(raw))
    if tasks is not None:
        config.p115.strm.tasks[:] = _tasks(json.loads(tasks))
    if not config.path or (raw is None and tasks is None and Path(config.path).exists()):
        return
    try:
        _write(config)
    except SettingsError as exc:
        log.warning("%s；網頁設定暫時只存在記憶體", exc)
        return
    if raw is not None or tasks is not None:
        log.info("已把網頁上的設定搬進設定檔 %s", config.path)
        db.execute("DELETE FROM meta WHERE key IN (?, ?)", (SETTINGS_META_KEY, TASKS_META_KEY))


def reload_if_changed(config: Config) -> bool:
    """設定檔被手動改過時重新讀取，就地套用；有變動回傳 True。"""
    if not config.path:
        return False
    path = Path(config.path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    if mtime == config.file_mtime:
        return False
    try:
        new = read_file(path)
        apply_settings(copy.deepcopy(config), export_settings(new))
    except (ValueError, OSError) as exc:
        raise SettingsError(f"設定檔有錯，沒有套用：{exc}") from exc
    apply_settings(config, export_settings(new))
    # 這些只在啟動時使用，先記下來；server 的 host、port、data_dir 要重新啟動才生效
    config.users[:] = new.users
    config.api_keys[:] = new.api_keys
    config.p115.cookies = new.p115.cookies
    config.p115.timeout = new.p115.timeout
    config.file_mtime = mtime
    log.info("設定檔 %s 被修改，已重新套用", path)
    return True


def save(db: Database, config: Config, raw: dict) -> None:
    """驗證並寫進設定檔，成功後才套用到執行中的設定。"""
    reload_if_changed(config)  # 先接上手動改過的內容，免得被網頁的舊內容蓋掉
    trial = copy.deepcopy(config)
    apply_settings(trial, raw)  # 先在副本上驗證，失敗時不留下改一半的設定
    _write(trial)
    apply_settings(config, raw)
    config.file_mtime = trial.file_mtime


def refresh(st) -> None:
    """設定檔被手動改過時重新套用，並處理跟著要做的事（重新掃描、115 設定）。"""
    before = export_settings(st.config)["libraries"]
    if reload_if_changed(st.config):
        after_change(st, before)


def after_change(st, libraries_before: list) -> None:
    logs.set_level(st.config.server.log_level)
    # P115Service 建立時複製了這兩個值，要同步過去
    st.p115.app = st.config.p115.app
    st.p115.open.default_app_id = st.config.p115.open_app_id
    st.strm_sync.prune_index()
    after = export_settings(st.config)["libraries"]
    if after != libraries_before:
        # 只掃新增或改過的媒體庫；刪掉的媒體庫，它的項目在掃描時一起移除
        changed = [lib["name"] for lib in after if lib not in libraries_before]
        threading.Thread(target=st.scanner.scan_libraries, args=(changed,), daemon=True).start()
