"""PlaybackInfo、串流（strm 302）與播放進度回報。"""

from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse

from .. import logs
from ..auth import AuthContext, now_iso, require_user
from ..dto import media_source_dto
from .common import q, q_int, state
from .items import _set_user_data

log = logging.getLogger(__name__)
router = APIRouter()

# 這些路徑名稱不是影片本體，不能被當成串流處理
NON_MEDIA_NAMES = {
    "additionalparts", "subtitles", "similar", "thememedia", "themevideos",
    "themesongs", "specialfeatures", "linkeditems", "playbackinfo",
}


@router.api_route("/items/{item_id}/playbackinfo", methods=["GET", "POST"])
def playback_info(item_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    st = state(request)
    row = st.db.get_item(item_id)
    if not row or row["type"] not in ("Movie", "Episode"):
        raise HTTPException(status_code=404, detail="Item not found")
    remote = st.redirector.display_target(row, str(request.base_url))
    ms = media_source_dto(row, remote, ctx.token)
    ms_id = q(request, "MediaSourceId")
    if ms_id and ms_id != ms["Id"]:
        log.debug("PlaybackInfo MediaSourceId 不符：%s", ms_id)
    return {
        "MediaSources": [ms],
        "PlaySessionId": f"{item_id}-{ctx.user_id[:8]}",
    }


def _stream(item_id: str, name: str, request: Request):
    if name.split(".", 1)[0] in NON_MEDIA_NAMES:
        raise HTTPException(status_code=404, detail="Not found")
    st = state(request)
    if st.config.redirect.require_auth:
        require_user(request)
    row = st.db.get_item(item_id)
    if not row or row["type"] not in ("Movie", "Episode"):
        raise HTTPException(status_code=404, detail="Item not found")

    if row["is_strm"]:
        headers = {k.lower(): v for k, v in request.headers.items()}
        url = st.redirector.final_url(row, headers)
        if url:
            log.info(
                "播放 %s：302 到 %s（%s）",
                Path(row["path"]).name, urlsplit(url).netloc or url[:60], request.headers.get("user-agent", "")[:60],
            )
            log.debug("302 完整網址：%s", logs.redact(url))
            return RedirectResponse(url=url, status_code=302)
        # strm 裡寫的是本機路徑：直接送檔
        target = st.redirector.strm_target(row)
        if target and Path(target).is_file():
            return _file(target)
        raise HTTPException(status_code=404, detail="strm 內容無效")
    return _file(row["path"])


def _file(path: str) -> FileResponse:
    media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@router.api_route("/videos/{item_id}/{name}", methods=["GET", "HEAD"])
def video_stream(item_id: str, name: str, request: Request):
    return _stream(item_id, name, request)


@router.api_route("/items/{item_id}/download", methods=["GET", "HEAD"])
@router.api_route("/items/{item_id}/file", methods=["GET", "HEAD"])
def item_download(item_id: str, request: Request):
    return _stream(item_id, "stream", request)


# ---------------- 播放進度 ----------------


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _report(request: Request, ctx: AuthContext, body: dict, stopped: bool) -> Response:
    lb = {k.lower(): v for k, v in body.items()}
    item_id = lb.get("itemid") or q(request, "ItemId")
    if not item_id:
        return Response(status_code=204)
    st = state(request)
    row = st.db.get_item(item_id)
    if not row:
        return Response(status_code=204)
    pos = lb.get("positionticks")
    if pos is None:
        pos = q_int(request, "PositionTicks")
    pos = int(pos or 0)
    fields = {"last_played": now_iso(), "position_ticks": pos}
    runtime: Optional[int] = row["runtime_ticks"] or lb.get("runtimeticks")
    finished = bool(stopped and runtime and pos >= runtime * 0.9)
    if finished:
        fields.update(played=1, position_ticks=0)
    _set_user_data(request, ctx, item_id, **fields)
    if finished:
        st.db.execute(
            "UPDATE user_data SET play_count=play_count+1 WHERE user_id=? AND item_id=?",
            (ctx.user_id, row["id"]),
        )
    return Response(status_code=204)


@router.post("/sessions/playing")
async def playing_start(request: Request, ctx: AuthContext = Depends(require_user)):
    return _report(request, ctx, await _json_body(request), stopped=False)


@router.post("/sessions/playing/progress")
async def playing_progress(request: Request, ctx: AuthContext = Depends(require_user)):
    return _report(request, ctx, await _json_body(request), stopped=False)


@router.post("/sessions/playing/stopped")
async def playing_stopped(request: Request, ctx: AuthContext = Depends(require_user)):
    return _report(request, ctx, await _json_body(request), stopped=True)


@router.post("/sessions/playing/ping")
def playing_ping():
    return Response(status_code=204)


# 舊版 API：/Users/{uid}/PlayingItems/{id}
@router.post("/users/{user_id}/playingitems/{item_id}")
def legacy_start(user_id: str, item_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    return _report(request, ctx, {"ItemId": item_id}, stopped=False)


@router.post("/users/{user_id}/playingitems/{item_id}/progress")
def legacy_progress(user_id: str, item_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    return _report(request, ctx, {"ItemId": item_id}, stopped=False)


@router.delete("/users/{user_id}/playingitems/{item_id}")
@router.post("/users/{user_id}/playingitems/{item_id}/delete")
def legacy_stop(user_id: str, item_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    return _report(request, ctx, {"ItemId": item_id}, stopped=True)
