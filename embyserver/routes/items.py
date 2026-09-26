"""媒體庫、項目查詢、使用者資料、圖片。"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse

from ..auth import AuthContext, now_iso, require_admin, require_user
from ..dto import episode_fallback_image, item_dto, query_result, user_data_dto
from ..scanner import image_ext
from .common import q, q_bool, q_int, q_list, state

log = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 30 * 1024 * 1024

router = APIRouter()

SORT_COLUMNS = {
    "sortname": "i.sort_name",
    "name": "i.sort_name",
    "datecreated": "i.date_created",
    "datelastcontentadded": "i.date_modified",
    "premieredate": "COALESCE(i.premiere_date, i.year)",
    "productionyear": "i.year",
    "communityrating": "i.community_rating",
    "criticrating": "i.community_rating",
    "runtime": "i.runtime_ticks",
    "random": "RANDOM()",
    "dateplayed": "u.last_played",
    "playcount": "u.play_count",
    "indexnumber": "i.index_number",
    "parentindexnumber": "i.parent_index_number",
    "airedepisodeorder": "i.sort_name",
}


def _dto(request: Request, ctx: AuthContext, row, full: bool = False) -> dict:
    st = state(request)
    fields = {f.lower() for f in q_list(request, "Fields")}
    return item_dto(
        st.db,
        st.server_id,
        row,
        user_id=ctx.user_id,
        token=ctx.token,
        with_media_sources=full or "mediasources" in fields,
        resolve_remote=lambda it: st.redirector.display_target(it, str(request.base_url)),
    )


def _libraries(request: Request) -> List[Any]:
    return state(request).db.query(
        "SELECT * FROM items WHERE type='CollectionFolder' ORDER BY id"
    )


def _user_id_from(request: Request, ctx: AuthContext, user_id: Optional[str]) -> None:
    if user_id and user_id.lower() != (ctx.user_id or "").lower():
        # 目前只允許使用自己的資料；管理員可以代查
        if not ctx.user["is_admin"]:
            raise HTTPException(status_code=403, detail="Forbidden")


# ---------------- 媒體庫 ----------------


@router.get("/users/{user_id}/views")
def user_views(user_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    items = [_dto(request, ctx, r) for r in _libraries(request)]
    return query_result(items, len(items))


@router.get("/library/mediafolders")
def media_folders(request: Request, ctx: AuthContext = Depends(require_user)):
    items = [_dto(request, ctx, r) for r in _libraries(request)]
    return query_result(items, len(items))


def _virtual_folders(request: Request) -> List[dict]:
    st = state(request)
    out = []
    for lib in _libraries(request):
        conf = next((l for l in st.config.libraries if l.name == lib["name"]), None)
        paths = conf.paths if conf else []
        out.append(
            {
                "Name": lib["name"],
                "Locations": paths,
                "CollectionType": lib["collection_type"],
                "ItemId": str(lib["id"]),
                "Id": str(lib["id"]),
                "LibraryOptions": {"PathInfos": [{"Path": p} for p in paths]},
            }
        )
    return out


@router.get("/library/virtualfolders")
def virtual_folders(request: Request, ctx: AuthContext = Depends(require_user)):
    return _virtual_folders(request)


# MoviePilot 等工具把 Mi302 當 Emby 媒體伺服器時會用到下面幾個端點


@router.get("/library/virtualfolders/query")
def virtual_folders_query(request: Request, ctx: AuthContext = Depends(require_user)):
    items = _virtual_folders(request)
    return {"Items": items, "TotalRecordCount": len(items)}


@router.get("/library/selectablemediafolders")
def selectable_media_folders(request: Request, ctx: AuthContext = Depends(require_user)):
    return [
        {
            "Name": f["Name"],
            "Id": f["Id"],
            "Guid": f["Id"],
            "SubFolders": [
                {"Name": p.rstrip("/").rsplit("/", 1)[-1] or p, "Id": f"{f['Id']}-{i}", "Path": p}
                for i, p in enumerate(f["Locations"])
            ],
        }
        for f in _virtual_folders(request)
    ]


@router.get("/items/counts")
def item_counts(request: Request, ctx: AuthContext = Depends(require_user)):
    rows = state(request).db.query("SELECT type, COUNT(*) AS c FROM items GROUP BY type")
    counts = {r["type"]: r["c"] for r in rows}
    return {
        "MovieCount": counts.get("Movie", 0),
        "SeriesCount": counts.get("Series", 0),
        "EpisodeCount": counts.get("Episode", 0),
        "ItemCount": sum(counts.get(t, 0) for t in ("Movie", "Series", "Episode")),
        **{k: 0 for k in (
            "GameCount", "ArtistCount", "ProgramCount", "GameSystemCount", "TrailerCount", "SongCount",
            "AlbumCount", "MusicVideoCount", "BoxSetCount", "BookCount",
        )},
    }


def _background(target, *args) -> Response:
    import threading

    threading.Thread(target=target, args=args, daemon=True).start()
    return Response(status_code=204)


@router.post("/items/{item_id}/refresh")
def item_refresh(item_id: str, request: Request, ctx: AuthContext = Depends(require_admin)):
    """只重新掃描這個項目：一部片、一部劇（季、集也是重掃整部劇），媒體庫則掃整個媒體庫。"""
    st = state(request)
    row = st.db.get_item(item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Item not found")
    if row["type"] == "CollectionFolder":
        return _background(st.scanner.scan_libraries, [row["name"]])
    return _background(st.scanner.scan_paths, [row["path"].split("#", 1)[0]])


@router.post("/library/media/updated")
async def library_media_updated(request: Request, ctx: AuthContext = Depends(require_admin)):
    """MoviePilot 等工具整理完檔案後通知：{"Updates": [{"Path": "...", "UpdateType": "Created"}]}，只掃那些路徑。"""
    st = state(request)
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        body = {}
    updates = body.get("Updates") if isinstance(body, dict) else None
    paths = [st.moviepilot.unmap_path(str(u["Path"])) for u in updates or [] if isinstance(u, dict) and u.get("Path")]
    if not paths:
        return _background(st.scanner.scan_all)
    return _background(st.scanner.scan_paths, paths)


# ---------------- 項目查詢 ----------------


def _query_items(request: Request, ctx: AuthContext, user_id: Optional[str] = None) -> dict:
    st = state(request)
    where: List[str] = []
    params: List[Any] = []
    parent_id = q(request, "ParentId")
    recursive = bool(q_bool(request, "Recursive"))
    types = [t.lower() for t in q_list(request, "IncludeItemTypes")]
    exclude = [t.lower() for t in q_list(request, "ExcludeItemTypes")]
    ids = q_list(request, "Ids")

    if ids:
        where.append(f"i.id IN ({','.join('?' for _ in ids)})")
        params += [int(x) if x.isdigit() else -1 for x in ids]
    elif parent_id:
        parent = st.db.get_item(parent_id)
        if not parent:
            return query_result([], 0)
        if parent["type"] == "CollectionFolder":
            if recursive:
                where.append("i.library_id=? AND i.type<>'CollectionFolder'")
            else:
                where.append("i.parent_id=? AND i.type<>'CollectionFolder'")
            params.append(parent["id"])
        elif parent["type"] == "Series":
            if recursive:
                where.append("i.series_id=?")
            else:
                where.append("i.parent_id=?")
            params.append(parent["id"])
        else:
            where.append("i.parent_id=?")
            params.append(parent["id"])
    elif not recursive and not types and not q(request, "SearchTerm"):
        # 沒有 ParentId 又非遞迴：回傳媒體庫本身（Emby 的根目錄行為）
        where.append("i.type='CollectionFolder'")
    else:
        where.append("i.type<>'CollectionFolder'")

    if types:
        where.append(f"lower(i.type) IN ({','.join('?' for _ in types)})")
        params += types
    if exclude:
        where.append(f"lower(i.type) NOT IN ({','.join('?' for _ in exclude)})")
        params += exclude
    if q_bool(request, "IsFolder") is not None:
        folder_types = "('CollectionFolder','Series','Season','Folder')"
        where.append(("i.type IN " if q_bool(request, "IsFolder") else "i.type NOT IN ") + folder_types)

    term = q(request, "SearchTerm")
    if term:
        where.append("(i.name LIKE ? OR i.original_title LIKE ?)")
        params += [f"%{term}%", f"%{term}%"]
    starts = q(request, "NameStartsWith")
    if starts:
        where.append("i.sort_name LIKE ?")
        params.append(starts.lower() + "%")
    years = [int(y) for y in q_list(request, "Years") if y.isdigit()]
    if years:
        where.append(f"i.year IN ({','.join('?' for _ in years)})")
        params += years
    for genre in q_list(request, "Genres"):
        where.append("i.genres LIKE ?")
        params.append(f'%"{genre}"%')

    filters = {f.lower() for f in q_list(request, "Filters")}
    is_played = q_bool(request, "IsPlayed")
    if "isplayed" in filters:
        is_played = True
    if "isunplayed" in filters:
        is_played = False
    if is_played is not None:
        where.append("COALESCE(u.played,0)=?")
        params.append(int(is_played))
    if q_bool(request, "IsFavorite") or "isfavorite" in filters:
        where.append("COALESCE(u.is_favorite,0)=1")
    if "isresumable" in filters:
        where.append("COALESCE(u.position_ticks,0)>0 AND COALESCE(u.played,0)=0")

    sql_where = " AND ".join(where) or "1=1"
    base = (
        "FROM items i LEFT JOIN user_data u ON u.item_id=i.id AND u.user_id=? "
        f"WHERE {sql_where}"
    )
    all_params = [ctx.user_id or ""] + params
    total = st.db.one(f"SELECT COUNT(*) AS c {base}", all_params)["c"]

    sort_by = [s.lower() for s in q_list(request, "SortBy")] or ["sortname"]
    orders = [o.lower() for o in q_list(request, "SortOrder")] or ["ascending"]
    order_parts = []
    for idx, key in enumerate(sort_by):
        col = SORT_COLUMNS.get(key)
        if not col:
            continue
        direction = orders[min(idx, len(orders) - 1)]
        order_parts.append(f"{col} {'DESC' if direction.startswith('desc') else 'ASC'}")
    order_parts.append("i.id ASC")
    sql = f"SELECT i.* {base} ORDER BY {', '.join(order_parts)}"
    start = q_int(request, "StartIndex", 0) or 0
    limit = q_int(request, "Limit")
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        all_params += [limit, start]
    elif start:
        sql += " LIMIT -1 OFFSET ?"
        all_params.append(start)
    rows = st.db.query(sql, all_params)
    return query_result([_dto(request, ctx, r) for r in rows], total, start)


@router.get("/users/{user_id}/items")
def user_items(user_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    _user_id_from(request, ctx, user_id)
    return _query_items(request, ctx, user_id)


@router.get("/items")
def items(request: Request, ctx: AuthContext = Depends(require_user)):
    return _query_items(request, ctx, q(request, "UserId"))


@router.get("/users/{user_id}/items/latest")
def latest(user_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    st = state(request)
    limit = q_int(request, "Limit", 20)
    parent_id = q(request, "ParentId")
    params: List[Any] = []
    where = "i.type IN ('Movie','Series')"
    if parent_id:
        where += " AND i.library_id=?"
        params.append(int(parent_id) if parent_id.isdigit() else -1)
    types = [t.lower() for t in q_list(request, "IncludeItemTypes")]
    if types:
        # 要求 Episode 時仍以劇集為單位回傳（與 Emby 的分組行為相近）
        mapped = {"series" if t == "episode" else t for t in types}
        where += f" AND lower(i.type) IN ({','.join('?' for _ in mapped)})"
        params += list(mapped)
    rows = st.db.query(
        f"SELECT i.* FROM items i WHERE {where} "
        "ORDER BY COALESCE(i.date_modified, i.date_created) DESC LIMIT ?",
        params + [limit],
    )
    return [_dto(request, ctx, r) for r in rows]


@router.get("/users/{user_id}/items/resume")
def resume(user_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    st = state(request)
    limit = q_int(request, "Limit", 20)
    rows = st.db.query(
        "SELECT i.* FROM items i JOIN user_data u ON u.item_id=i.id AND u.user_id=? "
        "WHERE u.position_ticks>0 AND u.played=0 AND i.type IN ('Movie','Episode') "
        "ORDER BY u.last_played DESC LIMIT ?",
        (ctx.user_id, limit),
    )
    items = [_dto(request, ctx, r) for r in rows]
    return query_result(items, len(items))


@router.get("/users/{user_id}/items/{item_id}")
def user_item(user_id: str, item_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    row = state(request).db.get_item(item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Item not found")
    return _dto(request, ctx, row, full=True)


@router.get("/items/{item_id}")
def item_get(item_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    return user_item("", item_id, request, ctx)


@router.get("/items/{item_id}/ancestors")
def ancestors(item_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    st = state(request)
    out = []
    row = st.db.get_item(item_id)
    while row and row["parent_id"] and row["parent_id"] != row["id"]:
        row = st.db.get_item(row["parent_id"])
        if row:
            out.append(_dto(request, ctx, row))
    return out


# ---------------- 劇集 ----------------


@router.get("/shows/{series_id}/seasons")
def seasons(series_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    st = state(request)
    rows = st.db.query(
        "SELECT * FROM items WHERE type='Season' AND series_id=? ORDER BY index_number",
        (int(series_id) if series_id.isdigit() else -1,),
    )
    items = [_dto(request, ctx, r) for r in rows]
    return query_result(items, len(items))


@router.get("/shows/{series_id}/episodes")
def episodes(series_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    st = state(request)
    params: List[Any] = [int(series_id) if series_id.isdigit() else -1]
    where = "type='Episode' AND series_id=?"
    season_id = q(request, "SeasonId")
    season_no = q_int(request, "Season")
    if season_id:
        where += " AND season_id=?"
        params.append(int(season_id) if season_id.isdigit() else -1)
    elif season_no is not None:
        where += " AND parent_index_number=?"
        params.append(season_no)
    rows = st.db.query(
        f"SELECT * FROM items WHERE {where} ORDER BY parent_index_number, index_number, sort_name",
        params,
    )
    start = q_int(request, "StartIndex", 0) or 0
    limit = q_int(request, "Limit")
    sliced = rows[start: start + limit] if limit is not None else rows[start:]
    fields = {f.lower() for f in q_list(request, "Fields")}
    items = [_dto(request, ctx, r, full="mediasources" in fields) for r in sliced]
    return query_result(items, len(rows), start)


@router.get("/shows/nextup")
def next_up(request: Request, ctx: AuthContext = Depends(require_user)):
    st = state(request)
    limit = q_int(request, "Limit", 20)
    series_filter = q(request, "SeriesId")
    params: List[Any] = [ctx.user_id]
    extra = ""
    if series_filter and series_filter.isdigit():
        extra = " AND i.series_id=?"
        params.append(int(series_filter))
    # 每部劇取最後看過的集數，下一集即為「接著看」
    last = st.db.query(
        "SELECT i.series_id, MAX(i.parent_index_number*100000 + COALESCE(i.index_number,0)) AS k, "
        "MAX(u.last_played) AS lp FROM items i JOIN user_data u ON u.item_id=i.id AND u.user_id=? "
        f"WHERE i.type='Episode' AND u.played=1{extra} GROUP BY i.series_id ORDER BY lp DESC",
        params,
    )
    out = []
    for r in last:
        nxt = st.db.one(
            "SELECT * FROM items WHERE type='Episode' AND series_id=? "
            "AND parent_index_number*100000 + COALESCE(index_number,0) > ? "
            "ORDER BY parent_index_number, index_number LIMIT 1",
            (r["series_id"], r["k"]),
        )
        if nxt:
            out.append(_dto(request, ctx, nxt))
        if len(out) >= limit:
            break
    return query_result(out, len(out))


# ---------------- 使用者資料 ----------------


def _set_user_data(request: Request, ctx: AuthContext, item_id: str, **fields) -> dict:
    st = state(request)
    row = st.db.get_item(item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Item not found")
    st.db.execute(
        "INSERT OR IGNORE INTO user_data(user_id, item_id) VALUES(?, ?)", (ctx.user_id, row["id"])
    )
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        st.db.execute(
            f"UPDATE user_data SET {sets} WHERE user_id=? AND item_id=?",
            (*fields.values(), ctx.user_id, row["id"]),
        )
    return user_data_dto(st.db, ctx.user_id, row)


def mark_played(request: Request, ctx: AuthContext, item_id: str, played: bool) -> dict:
    st = state(request)
    row = st.db.get_item(item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Item not found")
    targets = [row]
    if row["type"] in ("Series", "Season"):
        col = "series_id" if row["type"] == "Series" else "season_id"
        targets = st.db.query(f"SELECT * FROM items WHERE {col}=? AND type='Episode'", (row["id"],))
    for t in targets:
        if played:
            _set_user_data(request, ctx, t["id"], played=1, position_ticks=0, last_played=now_iso())
            st.db.execute(
                "UPDATE user_data SET play_count=play_count+1 WHERE user_id=? AND item_id=?",
                (ctx.user_id, t["id"]),
            )
        else:
            _set_user_data(request, ctx, t["id"], played=0, position_ticks=0)
    return user_data_dto(st.db, ctx.user_id, row)


@router.post("/users/{user_id}/playeditems/{item_id}")
def played_add(user_id: str, item_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    return mark_played(request, ctx, item_id, True)


@router.delete("/users/{user_id}/playeditems/{item_id}")
@router.post("/users/{user_id}/playeditems/{item_id}/delete")
def played_remove(user_id: str, item_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    return mark_played(request, ctx, item_id, False)


@router.post("/users/{user_id}/favoriteitems/{item_id}")
def fav_add(user_id: str, item_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    return _set_user_data(request, ctx, item_id, is_favorite=1)


@router.delete("/users/{user_id}/favoriteitems/{item_id}")
@router.post("/users/{user_id}/favoriteitems/{item_id}/delete")
def fav_remove(user_id: str, item_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    return _set_user_data(request, ctx, item_id, is_favorite=0)


# ---------------- 空結果的周邊端點 ----------------


@router.get("/users/{user_id}/items/{item_id}/intros")
@router.get("/items/{item_id}/similar")
@router.get("/movies/{item_id}/similar")
@router.get("/shows/{item_id}/similar")
@router.get("/videos/{item_id}/additionalparts")
@router.get("/items/{item_id}/criticreviews")
def empty_query(request: Request):
    return query_result([], 0)


@router.get("/users/{user_id}/items/{item_id}/localtrailers")
@router.get("/users/{user_id}/items/{item_id}/specialfeatures")
@router.get("/items/{item_id}/specialfeatures")
def empty_list():
    return []


@router.get("/items/{item_id}/thememedia")
def theme_media():
    empty = {"OwnerId": "0", "Items": [], "TotalRecordCount": 0}
    return {"ThemeVideosResult": empty, "ThemeSongsResult": empty, "SoundtrackSongsResult": empty}


@router.get("/genres")
def genres(request: Request, ctx: AuthContext = Depends(require_user)):
    st = state(request)
    import json as _json

    names = set()
    for r in st.db.query("SELECT genres FROM items WHERE genres IS NOT NULL"):
        names.update(_json.loads(r["genres"]))
    items = [{"Name": n, "Id": n, "Type": "Genre", "ServerId": st.server_id} for n in sorted(names)]
    return query_result(items, len(items))


# ---------------- 圖片 ----------------

IMAGE_COLUMNS = {
    "primary": "primary_image",
    "backdrop": "backdrop_image",
    "thumb": "thumb_image",
    "logo": "logo_image",
    "art": "logo_image",
}


@router.api_route("/items/{item_id}/images/{image_type}", methods=["GET", "HEAD"])
@router.api_route("/items/{item_id}/images/{image_type}/{index}", methods=["GET", "HEAD"])
def item_image(item_id: str, image_type: str, request: Request, index: int = 0):
    st = state(request)
    row = st.db.get_item(item_id)
    col = IMAGE_COLUMNS.get(image_type.lower())
    if not row or not col:
        raise HTTPException(status_code=404, detail="Image not found")
    path = row[col]
    if not path and row["series_id"]:
        series = st.db.get_item(row["series_id"])
        if image_type.lower() != "primary":
            path = series[col] if series else None
        elif row["type"] == "Episode":
            path = episode_fallback_image(series)  # 沒有劇照的集，和 ImageTags 給的是同一張
    if not path:
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000"})


def _image_body(body: bytes) -> bytes:
    """Emby 的上傳圖片 API 收 base64 文字；也接受直接傳圖片檔。"""
    data = body.strip()
    if data.startswith(b"data:"):
        data = data.split(b",", 1)[-1]
    if image_ext(data):
        return data
    try:
        return base64.b64decode(data)
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=400, detail="圖片內容無法解讀")


@router.post("/items/{item_id}/images/{image_type}")
@router.post("/items/{item_id}/images/{image_type}/{index}")
async def upload_image(item_id: str, image_type: str, request: Request, ctx: AuthContext = Depends(require_admin)):
    """上傳圖片（例如 MoviePilot 的媒體庫封面插件）。存在資料夾 data/images，重新掃描時不會被蓋掉。"""
    st = state(request)
    row = st.db.get_item(item_id)
    col = IMAGE_COLUMNS.get(image_type.lower())
    if not row or not col:
        raise HTTPException(status_code=404, detail="Item not found")
    data = _image_body(await request.body())
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="圖片太大")
    try:
        st.scanner.save_custom_image(row["id"], col, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log.info("已更新「%s」的 %s 圖片（%s KB）", row["name"], image_type, len(data) // 1024)
    return Response(status_code=204)


@router.delete("/items/{item_id}/images/{image_type}")
@router.delete("/items/{item_id}/images/{image_type}/{index}")
def delete_image(item_id: str, image_type: str, request: Request, ctx: AuthContext = Depends(require_admin)):
    """刪掉上傳的圖片，改回媒體資料夾裡的圖。"""
    st = state(request)
    row = st.db.get_item(item_id)
    col = IMAGE_COLUMNS.get(image_type.lower())
    if not row or not col:
        raise HTTPException(status_code=404, detail="Item not found")
    st.scanner.remove_custom_image(row["id"], col)
    if row["type"] == "CollectionFolder":
        lib = next((l for l in st.config.libraries if l.name == row["name"]), None)
        cover = st.scanner.library_cover(lib, row["id"]) if lib else None
        st.db.execute("UPDATE items SET primary_image=? WHERE id=?", (cover, row["id"]))
        return Response(status_code=204)
    st.db.execute(f"UPDATE items SET {col}=NULL WHERE id=?", (row["id"],))
    return _background(st.scanner.scan_paths, [row["path"].split("#", 1)[0]])


@router.api_route("/users/{user_id}/images/{image_type}", methods=["GET", "HEAD"])
def user_image(user_id: str, image_type: str):
    return Response(status_code=404)
