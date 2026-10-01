"""照 MoviePilot 自己的整理規則算新名字（Mi302 整理助手的 POST /names）。

和 MoviePilot 手動整理同一套步驟、同一批函式，只是不跑整理流程：不查目標目錄、不抓圖、不寫整理紀錄、不碰 115。
1. 檔名解析成中繼資料（MetaInfoPath），套上指定的季、集數定位；
2. 認片：指定了 TMDB 編號就照編號查，沒指定就照檔名、資料夾名認（和它一樣會讀資料夾名裡的 tmdbid）；
   補 TMDB 資料，沿用它整理紀錄裡同一部片的片名（設定「刮削跟隨 TMDB」關掉時，它整理也是這樣）；
3. 劇集查這一季的集資料；
4. 照它的重命名格式算出新路徑（相對於媒體庫目錄），字幕加上語言標記。
同一部片、同一季在一次請求裡只認、只查一次，所以幾百集的資料夾也只要幾秒。
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

SKIP_MARKERS = ("/@Recycle/", "/#recycle/", "/.", "/@eaDir")  # 它整理時也跳過：回收站、隱藏的檔案
# 算名字用到的 MoviePilot 內部模組、類別和方法（有幾個是雙底線的私有方法，它改版時最容易不見）
INTERNALS = {
    "app.application.configuration": {"get_configured_system_config": []},
    "app.application.formatting": {"FormatParser": ["match", "split_episode"]},
    "app.runtime.settings": {"get_runtime_setting": []},
    "app.schemas.types": {"MediaType": [], "SystemConfigKey": [], "MediaSource": []},
    "app.domain.metainfo": {"MetaInfoPath": []},
    "app.modules.filemanager.transhandler": {"TransHandler": [
        "get_rename_path", "get_naming_dict", "_TransHandler__is_special_extra_file", "_TransHandler__rename_subtitles"]},
    "app.schemas.file": {"FileItem": []},
    "app.chain.media": {"MediaChain": ["recognize_media", "recognize_by_meta", "supplement_tmdb_info"]},
    "app.chain.transfer": {"TransferChain": []},
    "app.chain.tmdb": {"TmdbChain": ["tmdb_episodes"]},
}


def missing_internals() -> List[str]:
    """這版 MoviePilot 少了哪些算名字要用的東西（模組.名稱[.方法]）；都在是空的。缺了就不算名字，Mi302 改用它的整理預覽。"""
    import importlib

    missing: List[str] = []
    for module, names in INTERNALS.items():
        try:
            mod = importlib.import_module(module)
        except Exception:  # 模組搬走、改名，或載入時出錯
            missing.append(module)
            continue
        for name, methods in names.items():
            try:
                obj = getattr(mod, name, None)  # 有的模組用 __getattr__ 惰性匯入，這時才會出錯
            except Exception:
                obj = None
            if obj is None:
                missing.append(f"{module}.{name}")
                continue
            missing += [f"{module}.{name}.{m}" for m in methods if not hasattr(obj, m)]
    return missing


class Namer:
    """一次請求：同樣的指定（TMDB 編號、類型、季、集數定位）套在每個檔案上。"""

    def __init__(self, payload: dict) -> None:
        from app.application.configuration import get_configured_system_config
        from app.application.formatting import FormatParser
        from app.runtime.settings import get_runtime_setting
        from app.schemas.types import MediaType, SystemConfigKey

        self.MediaType = MediaType
        self.setting = get_runtime_setting
        self.mtype = {"电视剧": MediaType.TV, "电影": MediaType.MOVIE}.get(str(payload.get("type") or ""))
        tmdbid = str(payload.get("tmdbid") or "").strip()
        self.tmdbid = tmdbid if tmdbid.isdigit() else ""
        season = payload.get("season")
        self.season = int(season) if str(season if season is not None else "").strip().isdigit() else None
        fmt = str(payload.get("episode_format") or "").strip()
        self.parser = FormatParser(eformat=fmt) if fmt else None
        self.media_exts = {e.lower() for e in get_runtime_setting("RMT_MEDIAEXT") or []}
        self.sub_exts = {e.lower() for e in get_runtime_setting("RMT_SUBEXT") or []}
        self.audio_exts = {e.lower() for e in get_runtime_setting("RMT_AUDIOEXT") or []}
        self.exclude = [w for w in get_configured_system_config().get(SystemConfigKey.TransferExcludeWords) or [] if w]
        self._media: Dict[tuple, Any] = {}
        self._episodes: Dict[tuple, Any] = {}

    def run(self, items: List[dict]) -> List[dict]:
        """每個要整理的檔案一筆結果；不是影片、字幕、音軌的（nfo、圖片），它整理時也不管的，不回傳。"""
        out = []
        for it in items:
            path = str((it or {}).get("path") or "")
            name = PurePosixPath(path).name
            ext = PurePosixPath(name).suffix.lower()
            if not name or any(m in path for m in SKIP_MARKERS):
                continue
            kind = "media" if ext in self.media_exts else "sub" if ext in self.sub_exts else \
                "audio" if ext in self.audio_exts else ""
            if not kind or any(re.search(w, path, re.IGNORECASE) for w in self.exclude):
                continue
            if self.parser and not self.parser.match(name):
                continue  # 指定了集數定位：它只整理對得上的檔案
            try:
                out.append(self._one(it, path, name, kind))
            except Exception as exc:  # 一個檔案出錯不影響其他的
                out.append(_failed(path, f"{type(exc).__name__}: {exc}"))
        return out

    def _one(self, it: dict, path: str, name: str, kind: str) -> dict:
        from app.domain.metainfo import MetaInfoPath
        from app.modules.filemanager.transhandler import TransHandler
        from app.schemas.file import FileItem

        stem, suffix = PurePosixPath(name).stem, PurePosixPath(name).suffix
        fileitem = FileItem(storage="u115", type="file", path=path, name=name, basename=stem,
                            extension=suffix.lstrip("."), size=int(it.get("size") or 0), fileid=str(it.get("fileid") or ""))
        meta = MetaInfoPath(Path(path), force_video=True)
        if not meta:
            return _failed(path, "未识别到媒体信息")
        if self.season is not None:
            meta.begin_season = self.season
        if self.parser:
            begin, end, part = self.parser.split_episode(file_name=name, file_meta=meta)
            if begin is not None:
                meta.begin_episode = begin
            if part is not None:
                meta.part = part
            if end is not None:
                meta.end_episode = end
        mediainfo = self._recognize(meta)
        if not mediainfo:
            return _failed(path, "未识别到媒体信息")
        planning = deepcopy(meta)
        if mediainfo.type == self.MediaType.TV:
            if meta.begin_episode is None:
                special = TransHandler._TransHandler__is_special_extra_file(fileitem)
                return _failed(path, "未识别到文件集数，识别为特典/附加视频文件" if special else "未识别到文件集数", mediainfo, meta)
            planning.end_season = None
            if planning.total_season:
                planning.total_season = 1
            if (planning.total_episode or 0) > 2:
                planning.total_episode = 1
                planning.end_episode = None
        target = TransHandler.get_rename_path(
            template_string=self.setting("RENAME_FORMAT")(mediainfo.type),
            rename_dict=TransHandler.get_naming_dict(meta=planning, mediainfo=mediainfo, file_ext=suffix,
                                                     episodes_info=self._episodes_of(mediainfo, meta)),
            source_path=path,
            source_item=fileitem,
        )
        if kind == "sub":
            target = TransHandler._TransHandler__rename_subtitles(fileitem, target)
        if not target or not target.as_posix().strip("/"):
            return _failed(path, "未识别到新名称", mediainfo, meta)
        return {"path": path, "success": True, "target": target.as_posix(), "message": "", **_identity(mediainfo, meta)}

    def _recognize(self, meta) -> Optional[Any]:
        """認片（同一部、同一季只認一次）：指定了 TMDB 編號照編號查，沒有就照它整理時的認法。"""
        from app.chain.media import MediaChain
        from app.schemas.types import MediaSource

        if self.tmdbid:
            key = ("id", self.tmdbid, self.mtype, meta.begin_season)
        else:
            # 資料夾名裡的編號（{tmdbid=…}）解析在 media_source、media_id；舊版叫 tmdbid
            ident = (getattr(meta, "media_source", None), getattr(meta, "media_id", None), getattr(meta, "tmdbid", None))
            key = ("meta", ident, meta.name, meta.year, meta.type, meta.begin_season)
        if key not in self._media:
            chain = MediaChain()
            if self.tmdbid:
                info = chain.recognize_media(mtype=self.mtype, media_source=MediaSource.TMDB, media_id=self.tmdbid)
            elif self.mtype:
                info = chain.recognize_by_meta(meta, mtype=self.mtype)
            else:
                info = chain.recognize_by_meta(meta)
            if info:
                info = chain.supplement_tmdb_info(info, meta) or info
                self._history_title(info)
            self._media[key] = info
        return self._media[key]

    @staticmethod
    def _history_title(info) -> None:
        """它整理時：沒開「刮削跟隨 TMDB」的話，同一部片沿用第一次整理紀錄的片名，資料夾才不會分成兩個。"""
        from app.chain.transfer import TransferChain
        from app.schemas.types import MediaSource

        chain = TransferChain()
        if chain.runtime_config.scrape_follow_tmdb or info.media_source != MediaSource.TMDB:
            return
        history = chain.transfer_history_repository.get_by_media_identity(
            media_source=info.media_source.value, media_id=info.media_id, mtype=info.type.value)
        if history and history.title and info.title != history.title:
            info.title = history.title

    def _episodes_of(self, info, meta) -> Optional[list]:
        """劇集這一季的集資料（集名、播出日期給重命名格式用）；同一季只查一次。"""
        from app.chain.tmdb import TmdbChain

        if info.type != self.MediaType.TV or not info.tmdb_id:
            return None
        season = info.season
        if season is None and meta.season_seq and str(meta.season_seq).isdigit():
            season = int(meta.season_seq)
        season = 1 if season is None else season
        key = (info.tmdb_id, season, info.episode_group)
        if key not in self._episodes:
            self._episodes[key] = TmdbChain().tmdb_episodes(tmdbid=info.tmdb_id, season=season,
                                                            episode_group=info.episode_group)
        return self._episodes[key]


def _identity(mediainfo, meta) -> dict:
    """和它整理預覽回的一樣：認成什麼（片名帶年份）、類型、季、集。"""
    return {"title": mediainfo.title_year if mediainfo else None,
            "type": mediainfo.type.value if mediainfo and mediainfo.type else None,
            "season": meta.begin_season if meta else None, "episode": meta.begin_episode if meta else None}


def _failed(path: str, message: str, mediainfo=None, meta=None) -> dict:
    return {"path": path, "success": False, "target": None, "message": message, **_identity(mediainfo, meta)}
