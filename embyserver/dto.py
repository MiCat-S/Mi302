"""把資料庫列轉成 Emby 的 BaseItemDto / UserDto JSON。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .db import Database
from .mediainfo import MediaInfoStore, default_audio_index, primary_video

VIDEO_TYPES = {"Movie", "Episode"}
FOLDER_TYPES = {"CollectionFolder", "Series", "Season", "Folder"}


def image_tag(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return hashlib.md5(f"{path}:{mtime}".encode()).hexdigest()


def episode_fallback_image(series) -> Optional[str]:
    """單集沒有劇照時用的圖：劇的橫幅（thumb/landscape），沒有就用背景圖，兩者都是 16:9。"""
    if not series:
        return None
    return series["thumb_image"] or series["backdrop_image"]


def media_source_id(item: sqlite3.Row) -> str:
    return hashlib.md5(f"ms:{item['id']}:{item['path']}".encode()).hexdigest()


class Prefetch:
    """列表一次查好整頁要用的：使用者資料、所屬的劇和季、子項數、沒看過的集數。

    item_dto 帶著它就不必每個項目各查三五次（一頁 100 集原本要三四百次，資料庫是單一連線加鎖，
    查詢多了會卡住其他請求）。每種資料用 IN (...) 分批查，一批最多 CHUNK 個 id；結果和逐項查完全一樣。
    """

    CHUNK = 500

    def __init__(self, db: Database, rows: List[sqlite3.Row], user_id: Optional[str]):
        self.user_id = user_id
        by_type: Dict[str, List[int]] = {}
        for r in rows:
            by_type.setdefault(r["type"], []).append(r["id"])
        self._user_data: Dict[int, sqlite3.Row] = {}
        if user_id:
            for r in self._select(db, "SELECT * FROM user_data WHERE user_id=? AND item_id IN ({})",
                                  [r["id"] for r in rows], (user_id,)):
                self._user_data[r["item_id"]] = r
        related = {r["series_id"] for r in rows if r["type"] in ("Season", "Episode") and r["series_id"]}
        related |= {r["season_id"] for r in rows if r["type"] == "Episode" and r["season_id"]}
        self._items = {r["id"]: r for r in self._select(db, "SELECT * FROM items WHERE id IN ({})", sorted(related))}
        # 子項數：劇、季數的是集；媒體庫和其他資料夾數直接放在底下的項目（和 _child_count 一樣）
        self._children: Dict[Tuple[str, int], int] = {}
        self._unplayed: Dict[Tuple[str, int], int] = {}
        for kind, col in (("Series", "series_id"), ("Season", "season_id")):
            ids = by_type.get(kind, [])
            for r in self._select(db, f"SELECT {col} AS k, COUNT(*) AS c FROM items WHERE type='Episode' AND {col} IN ({{}}) "
                                      f"GROUP BY {col}", ids):
                self._children[(kind, r["k"])] = r["c"]
            if user_id:
                for r in self._select(db, f"SELECT i.{col} AS k, COUNT(*) AS c FROM items i LEFT JOIN user_data u "
                                          f"ON u.item_id=i.id AND u.user_id=? WHERE i.{col} IN ({{}}) AND i.type='Episode' "
                                          f"AND COALESCE(u.played,0)=0 GROUP BY i.{col}", ids, (user_id,)):
                    self._unplayed[(kind, r["k"])] = r["c"]
        others = [i for kind, ids in by_type.items() if kind in FOLDER_TYPES - {"Series", "Season"} for i in ids]
        for r in self._select(db, "SELECT parent_id AS k, COUNT(*) AS c FROM items WHERE parent_id IN ({}) AND id<>parent_id "
                                  "GROUP BY parent_id", others):
            self._children[("", r["k"])] = r["c"]

    @classmethod
    def _select(cls, db: Database, sql: str, ids: List[int], before: Tuple = ()) -> List[sqlite3.Row]:
        out: List[sqlite3.Row] = []
        for i in range(0, len(ids), cls.CHUNK):
            chunk = ids[i:i + cls.CHUNK]
            out += db.query(sql.format(",".join("?" * len(chunk))), (*before, *chunk))
        return out

    def user_data(self, item_id: int) -> Optional[sqlite3.Row]:
        return self._user_data.get(item_id)

    def item(self, item_id: int) -> Optional[sqlite3.Row]:
        return self._items.get(item_id)

    def child_count(self, item: sqlite3.Row) -> int:
        kind = item["type"] if item["type"] in ("Series", "Season") else ""
        return self._children.get((kind, item["id"]), 0)

    def unplayed(self, item: sqlite3.Row) -> int:
        return self._unplayed.get((item["type"], item["id"]), 0)


def user_data_dto(db: Database, user_id: Optional[str], item: sqlite3.Row,
                  prefetch: Optional[Prefetch] = None) -> Dict[str, Any]:
    row = None
    if user_id:
        row = prefetch.user_data(item["id"]) if prefetch else db.one(
            "SELECT * FROM user_data WHERE user_id=? AND item_id=?", (user_id, item["id"])
        )
    data: Dict[str, Any] = {
        "PlaybackPositionTicks": row["position_ticks"] if row else 0,
        "PlayCount": row["play_count"] if row else 0,
        "IsFavorite": bool(row["is_favorite"]) if row else False,
        "Played": bool(row["played"]) if row else False,
        "Key": str(item["id"]),
    }
    if row and row["last_played"]:
        data["LastPlayedDate"] = row["last_played"]
    if item["type"] in ("Series", "Season") and user_id:
        data["UnplayedItemCount"] = unplayed = prefetch.unplayed(item) if prefetch else _unplayed_count(db, user_id, item)
        data["Played"] = unplayed == 0 and _child_count(db, item, prefetch) > 0
    return data


def _unplayed_count(db: Database, user_id: str, item: sqlite3.Row) -> int:
    col = "series_id" if item["type"] == "Series" else "season_id"
    return db.one(
        f"SELECT COUNT(*) AS c FROM items i LEFT JOIN user_data u "
        f"ON u.item_id=i.id AND u.user_id=? WHERE i.{col}=? AND i.type='Episode' "
        f"AND COALESCE(u.played,0)=0",
        (user_id, item["id"]),
    )["c"]


def _child_count(db: Database, item: sqlite3.Row, prefetch: Optional[Prefetch] = None) -> int:
    if prefetch:
        return prefetch.child_count(item)
    if item["type"] == "Series":
        return db.one("SELECT COUNT(*) AS c FROM items WHERE series_id=? AND type='Episode'", (item["id"],))["c"]
    if item["type"] == "Season":
        return db.one("SELECT COUNT(*) AS c FROM items WHERE season_id=? AND type='Episode'", (item["id"],))["c"]
    return db.one("SELECT COUNT(*) AS c FROM items WHERE parent_id=? AND id<>?", (item["id"], item["id"]))["c"]


def media_source_dto(
    item: sqlite3.Row, remote_url: Optional[str], token: Optional[str], info: Optional[dict] = None
) -> Dict[str, Any]:
    """組 MediaSourceInfo。strm 以 Http + IsRemote 呈現，與 Emby 解析 strm 後的樣子一致。

    info 是媒體資訊（X-mediainfo.json）：有的話補上媒體流、碼率、大小、片長，播放器才看得到解析度、
    HDR、音軌和字幕軌。播放方式（Path、DirectStreamUrl、不轉碼）維持 302 直連不變。
    """
    msid = media_source_id(item)
    container = item["container"] or "mkv"
    is_remote = bool(remote_url and remote_url.startswith(("http://", "https://")))
    stream_url = f"/videos/{item['id']}/stream.{container}?Static=true&MediaSourceId={msid}"
    if token:
        stream_url += f"&api_key={token}"
    ms = {
        "Protocol": "Http" if is_remote else "File",
        "Id": msid,
        "Path": remote_url if is_remote else item["path"],
        "Type": "Default",
        "Container": container,
        "Size": item["size"],
        "Name": os.path.splitext(os.path.basename(item["path"]))[0],
        "IsRemote": is_remote,
        "HasMixedProtocols": False,
        "RunTimeTicks": item["runtime_ticks"],
        "SupportsTranscoding": False,
        "SupportsDirectStream": True,
        "SupportsDirectPlay": True,
        "IsInfiniteStream": False,
        "RequiresOpening": False,
        "RequiresClosing": False,
        "RequiresLooping": False,
        "SupportsProbing": False,
        "MediaStreams": [],
        "Formats": [],
        "RequiredHttpHeaders": {},
        "DirectStreamUrl": stream_url,
        "AddApiKeyToDirectStreamUrl": False,
        "ReadAtNativeFramerate": False,
    }
    if info:
        src = info["source"]
        ms["MediaStreams"] = src.get("MediaStreams") or []
        if src.get("Container"):
            ms["Container"] = src["Container"]
        for key in ("Bitrate", "Size", "RunTimeTicks"):
            if not ms.get(key) and src.get(key):
                ms[key] = src[key]
        audio = default_audio_index(info)
        if audio is not None:
            ms["DefaultAudioStreamIndex"] = audio
    return ms


# 項目本身的欄位（資料庫欄位 → Emby 的欄位），有值才給
OPTIONAL_FIELDS = (
    ("OriginalTitle", "original_title"),
    ("Overview", "overview"),
    ("ProductionYear", "year"),
    ("PremiereDate", "premiere_date"),
    ("CommunityRating", "community_rating"),
    ("OfficialRating", "official_rating"),
    ("IndexNumber", "index_number"),
    ("ParentIndexNumber", "parent_index_number"),
    ("RunTimeTicks", "runtime_ticks"),
    ("Container", "container"),
)


def item_dto(
    db: Database,
    server_id: str,
    item: sqlite3.Row,
    user_id: Optional[str] = None,
    token: Optional[str] = None,
    with_media_sources: bool = False,
    resolve_remote=None,
    people=None,
    with_people: bool = False,
    intro=None,
    with_chapters: bool = False,
    can_download: bool = True,
    prefetch: Optional[Prefetch] = None,
) -> Dict[str, Any]:
    """一個項目的 BaseItemDto。欄位照區塊組：基本資料 → 圖片 → 所屬的劇／季 → 資料夾 → 使用者資料 → 演職人員 → 媒體資訊。

    prefetch：列表端點先一次查好整頁的使用者資料、劇和季、子項數（見 Prefetch）；沒給就逐項查。
    """
    t = item["type"]
    get_item = prefetch.item if prefetch else db.get_item
    dto = _common_fields(server_id, item, can_download)
    tags = _image_fields(dto, item)
    if t in ("Season", "Episode") and item["series_id"]:
        _series_fields(get_item(item["series_id"]), dto, item, tags)
    if t == "Episode" and item["season_id"]:
        _season_fields(get_item(item["season_id"]), dto)
    if t in FOLDER_TYPES:
        _folder_fields(db, dto, item, prefetch)

    dto["UserData"] = user_data_dto(db, user_id, item, prefetch)

    if with_people and people is not None and t in ("Movie", "Series", "Season", "Episode"):
        dto["People"] = people.for_item(item)

    if (with_media_sources or with_chapters) and t in VIDEO_TYPES:
        _media_fields(db, dto, item, token, with_media_sources, resolve_remote, intro)
    return {k: v for k, v in dto.items() if v is not None}


def _common_fields(server_id: str, item: sqlite3.Row, can_download: bool) -> Dict[str, Any]:
    """名稱、類型、父項目、有值的欄位、類型、外部 id、影片的路徑。"""
    t = item["type"]
    dto: Dict[str, Any] = {
        "Name": item["name"],
        "ServerId": server_id,
        "Id": str(item["id"]),
        "Etag": image_tag(item["path"]) if t in VIDEO_TYPES else None,
        "DateCreated": item["date_created"],
        "SortName": item["sort_name"],
        "Type": t,
        "IsFolder": t in FOLDER_TYPES,
        "LocationType": "FileSystem",
        "CanDelete": False,
        "CanDownload": can_download and t in VIDEO_TYPES,
        "SupportsSync": False,
    }
    if item["collection_type"]:
        dto["CollectionType"] = item["collection_type"]
    if item["parent_id"]:
        dto["ParentId"] = str(item["parent_id"])
    for key, col in OPTIONAL_FIELDS:
        if item[col] is not None:
            dto[key] = item[col]
    dto["Genres"] = json.loads(item["genres"]) if item["genres"] else []
    dto["GenreItems"] = [{"Name": g, "Id": hashlib.md5(g.encode()).hexdigest()[:8]} for g in dto["Genres"]]
    dto["ProviderIds"] = json.loads(item["provider_ids"]) if item["provider_ids"] else {}

    if t in VIDEO_TYPES:
        dto["MediaType"] = "Video"
        dto["VideoType"] = "VideoFile"
        dto["Path"] = item["path"]
        dto["HasSubtitles"] = False
    if t in ("CollectionFolder",):
        dto["Path"] = item["path"]
    return dto


def _image_fields(dto: Dict[str, Any], item: sqlite3.Row) -> Dict[str, str]:
    """項目自己的圖片；回傳 ImageTags（單集沒有劇照時，_series_fields 會補上劇的圖）。"""
    tags: Dict[str, str] = {}
    for key, col in (("Primary", "primary_image"), ("Thumb", "thumb_image"), ("Logo", "logo_image")):
        tag = image_tag(item[col])
        if tag:
            tags[key] = tag
    dto["ImageTags"] = tags
    backdrop = image_tag(item["backdrop_image"])
    dto["BackdropImageTags"] = [backdrop] if backdrop else []
    if tags.get("Primary"):
        dto["PrimaryImageAspectRatio"] = 16 / 9 if item["type"] == "Episode" else 2 / 3
    return tags


def _series_fields(series: Optional[sqlite3.Row], dto: Dict[str, Any], item: sqlite3.Row, tags: Dict[str, str]) -> None:
    """季、集所屬的劇：名稱，以及從劇借來的海報、背景、橫幅、標誌。"""
    if not series:
        return
    dto["SeriesId"] = str(series["id"])
    dto["SeriesName"] = series["name"]
    stag = image_tag(series["primary_image"])
    if stag:
        dto["SeriesPrimaryImageTag"] = stag
    btag = image_tag(series["backdrop_image"])
    if btag:
        dto["ParentBackdropItemId"] = str(series["id"])
        dto["ParentBackdropImageTags"] = [btag]
    ttag = image_tag(series["thumb_image"])
    if ttag:
        dto["ParentThumbItemId"] = str(series["id"])
        dto["ParentThumbImageTag"] = ttag
    if item["type"] == "Episode" and not tags.get("Primary"):
        # TMDB 沒有這集的劇照：用劇的橫幅圖頂上（/Images/Primary 也回同一張），播放器才不會一片空白
        ftag = image_tag(episode_fallback_image(series))
        if ftag:
            tags["Primary"] = ftag
            dto["PrimaryImageAspectRatio"] = 16 / 9
    ltag = image_tag(series["logo_image"])
    if ltag:
        dto["ParentLogoItemId"] = str(series["id"])
        dto["ParentLogoImageTag"] = ltag


def _season_fields(season: Optional[sqlite3.Row], dto: Dict[str, Any]) -> None:
    if season:
        dto["SeasonId"] = str(season["id"])
        dto["SeasonName"] = season["name"]


def _folder_fields(db: Database, dto: Dict[str, Any], item: sqlite3.Row, prefetch: Optional[Prefetch]) -> None:
    dto["ChildCount"] = _child_count(db, item, prefetch)
    if item["type"] == "Series":
        dto["RecursiveItemCount"] = dto["ChildCount"]
        # nfo 的 <status>（Continuing／Ended）掃描時沒有讀進資料庫，一律回 Continuing：
        # 播放器只拿它顯示「連載中」，不影響播放；要讀的話 scanner.parse_nfo 和 items 表都得加欄位
        dto["Status"] = "Continuing"


def _media_fields(db: Database, dto: Dict[str, Any], item: sqlite3.Row, token: Optional[str], with_media_sources: bool,
                  resolve_remote, intro) -> None:
    """影片的 MediaSources、媒體流、解析度和章節（含學到的片頭片尾標記）。"""
    info = MediaInfoStore(db).get(item["path"])
    if with_media_sources:
        remote = resolve_remote(item) if resolve_remote else None
        ms = media_source_dto(item, remote, token, info)
        dto["MediaSources"] = [ms]
        dto["MediaStreams"] = ms["MediaStreams"]
        if info:
            video = primary_video(info)
            if video:
                dto["Width"], dto["Height"] = video.get("Width"), video.get("Height")
            dto["HasSubtitles"] = any(s.get("Type") == "Subtitle" for s in ms["MediaStreams"])
            if not dto.get("RunTimeTicks") and ms.get("RunTimeTicks"):
                dto["RunTimeTicks"] = ms["RunTimeTicks"]
    # 媒體資訊裡的章節，加上學到的片頭片尾標記（Emby 的 MarkerType）
    chapters = list((info or {}).get("chapters") or [])
    if intro is not None:
        chapters += intro.chapters_for(item)
    if chapters or with_media_sources:
        dto["Chapters"] = chapters


def user_dto(user: dict, server_id: str, can_download: bool = True) -> Dict[str, Any]:
    is_admin = bool(user["is_admin"])
    return {
        "Name": user["name"],
        "ServerId": server_id,
        "Id": user["id"],
        "HasPassword": bool(user["password_hash"]),
        "HasConfiguredPassword": bool(user["password_hash"]),
        "HasConfiguredEasyPassword": False,
        "EnableAutoLogin": False,
        "LastLoginDate": user.get("last_login"),
        "LastActivityDate": user.get("last_activity"),
        "Configuration": {
            "PlayDefaultAudioTrack": True,
            "DisplayMissingEpisodes": False,
            "SubtitleMode": "Default",
            "EnableLocalPassword": False,
            "OrderedViews": [],
            "LatestItemsExcludes": [],
            "MyMediaExcludes": [],
            "HidePlayedInLatest": True,
            "RememberAudioSelections": True,
            "RememberSubtitleSelections": True,
            "EnableNextEpisodeAutoPlay": True,
        },
        "Policy": {
            "IsAdministrator": is_admin,
            "IsHidden": False,
            "IsHiddenRemotely": False,
            "IsDisabled": False,
            "EnableUserPreferenceAccess": True,
            "EnableRemoteControlOfOtherUsers": is_admin,
            "EnableSharedDeviceControl": True,
            "EnableRemoteAccess": True,
            "EnableLiveTvManagement": False,
            "EnableLiveTvAccess": False,
            "EnableMediaPlayback": True,
            "EnableAudioPlaybackTranscoding": False,
            "EnableVideoPlaybackTranscoding": False,
            "EnablePlaybackRemuxing": False,
            "EnableContentDeletion": False,
            "EnableContentDownloading": can_download,
            "EnableSubtitleDownloading": False,
            "EnableSubtitleManagement": False,
            "EnableSyncTranscoding": False,
            "EnableMediaConversion": False,
            "EnableAllDevices": True,
            "EnableAllChannels": True,
            "EnableAllFolders": True,
            "EnablePublicSharing": False,
            "InvalidLoginAttemptCount": 0,
            "RemoteClientBitrateLimit": 0,
            "SimultaneousStreamLimit": 0,
            "AllowCameraUpload": False,
        },
    }


def query_result(items: List[Dict[str, Any]], total: int, start: int = 0) -> Dict[str, Any]:
    return {"Items": items, "TotalRecordCount": total, "StartIndex": start}
