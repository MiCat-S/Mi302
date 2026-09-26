"""把資料庫列轉成 Emby 的 BaseItemDto / UserDto JSON。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

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


def user_data_dto(db: Database, user_id: Optional[str], item: sqlite3.Row) -> Dict[str, Any]:
    row = None
    if user_id:
        row = db.one(
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
        col = "series_id" if item["type"] == "Series" else "season_id"
        r = db.one(
            f"SELECT COUNT(*) AS c FROM items i LEFT JOIN user_data u "
            f"ON u.item_id=i.id AND u.user_id=? WHERE i.{col}=? AND i.type='Episode' "
            f"AND COALESCE(u.played,0)=0",
            (user_id, item["id"]),
        )
        data["UnplayedItemCount"] = r["c"]
        data["Played"] = r["c"] == 0 and _child_count(db, item) > 0
    return data


def _child_count(db: Database, item: sqlite3.Row) -> int:
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


def item_dto(
    db: Database,
    server_id: str,
    item: sqlite3.Row,
    user_id: Optional[str] = None,
    token: Optional[str] = None,
    with_media_sources: bool = False,
    resolve_remote=None,
) -> Dict[str, Any]:
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
        "CanDownload": t in VIDEO_TYPES,
        "SupportsSync": False,
    }
    if item["collection_type"]:
        dto["CollectionType"] = item["collection_type"]
    if item["parent_id"]:
        dto["ParentId"] = str(item["parent_id"])
    for key, col in (
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
    ):
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

    # 圖片
    tags: Dict[str, str] = {}
    for key, col in (("Primary", "primary_image"), ("Thumb", "thumb_image"), ("Logo", "logo_image")):
        tag = image_tag(item[col])
        if tag:
            tags[key] = tag
    dto["ImageTags"] = tags
    backdrop = image_tag(item["backdrop_image"])
    dto["BackdropImageTags"] = [backdrop] if backdrop else []
    if tags.get("Primary"):
        dto["PrimaryImageAspectRatio"] = 16 / 9 if t == "Episode" else 2 / 3

    if t in ("Season", "Episode") and item["series_id"]:
        series = db.get_item(item["series_id"])
        if series:
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
            if t == "Episode" and not tags.get("Primary"):
                # TMDB 沒有這集的劇照：用劇的橫幅圖頂上（/Images/Primary 也回同一張），播放器才不會一片空白
                ftag = image_tag(episode_fallback_image(series))
                if ftag:
                    tags["Primary"] = ftag
                    dto["PrimaryImageAspectRatio"] = 16 / 9
            ltag = image_tag(series["logo_image"])
            if ltag:
                dto["ParentLogoItemId"] = str(series["id"])
                dto["ParentLogoImageTag"] = ltag
    if t == "Episode" and item["season_id"]:
        season = db.get_item(item["season_id"])
        if season:
            dto["SeasonId"] = str(season["id"])
            dto["SeasonName"] = season["name"]
    if t in FOLDER_TYPES:
        dto["ChildCount"] = _child_count(db, item)
        if t == "Series":
            dto["RecursiveItemCount"] = dto["ChildCount"]
            dto["Status"] = "Continuing"

    dto["UserData"] = user_data_dto(db, user_id, item)

    if with_media_sources and t in VIDEO_TYPES:
        remote = resolve_remote(item) if resolve_remote else None
        info = MediaInfoStore(db).get(item["path"])
        ms = media_source_dto(item, remote, token, info)
        dto["MediaSources"] = [ms]
        dto["MediaStreams"] = ms["MediaStreams"]
        if info:
            video = primary_video(info)
            if video:
                dto["Width"], dto["Height"] = video.get("Width"), video.get("Height")
            dto["HasSubtitles"] = any(s.get("Type") == "Subtitle" for s in ms["MediaStreams"])
            dto["Chapters"] = info.get("chapters") or []
            if not dto.get("RunTimeTicks") and ms.get("RunTimeTicks"):
                dto["RunTimeTicks"] = ms["RunTimeTicks"]
    return {k: v for k, v in dto.items() if v is not None}


def user_dto(user: dict, server_id: str) -> Dict[str, Any]:
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
            "EnableContentDownloading": True,
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
