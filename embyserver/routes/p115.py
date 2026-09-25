"""115 掃碼登入與 strm 同步的管理 API，以及 pickcode 302 端點。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

import httpx

from ..auth import AuthContext, require_admin
from ..config import StrmTask
from ..p115 import PICKCODE_RE, P115Error
from ..p115_open import P115OpenError
from ..strm_sync import FULL, INCREMENTAL
from .common import q, state

router = APIRouter()


@router.get("/p115/status")
def p115_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    st = state(request)
    svc = st.p115
    # 管理員從哪個網址開這個頁面，播放器通常也連得到，拿來當 strm 裡的伺服器位址
    st.strm_sync.remember_base_url(str(request.base_url))
    account = svc.account_info(refresh=q(request, "refresh") in ("1", "true"))
    cookie = account.get("cookie") or {}
    return {
        **account,
        "cookie": bool(svc.cookies),
        "cookie_info": account.get("cookie"),
        # 舊欄位：cookie 有效時的帳號
        "user": {"user_id": cookie.get("user_id"), "user_name": cookie.get("user_name")} if cookie.get("valid") else None,
        "open": {**svc.open.status(), **(account.get("open") or {})},
    }


@router.post("/p115/open/qrcode")
async def p115_open_qrcode(request: Request, ctx: AuthContext = Depends(require_admin)):
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        body = {}
    try:
        return state(request).p115.open.qrcode_start(str(body.get("app_id") or ""))
    except (P115OpenError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/p115/open/qrcode/status")
def p115_open_qrcode_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    uid, time_, sign = q(request, "uid"), q(request, "time"), q(request, "sign")
    if not uid:
        raise HTTPException(status_code=400, detail="缺少 uid")
    try:
        return state(request).p115.open.qrcode_status(uid, time_ or "", sign or "")
    except (P115OpenError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/p115/open/logout")
def p115_open_logout(request: Request, ctx: AuthContext = Depends(require_admin)):
    state(request).p115.open.logout()
    return Response(status_code=204)


@router.post("/p115/qrcode")
def p115_qrcode(request: Request, ctx: AuthContext = Depends(require_admin)):
    try:
        return state(request).p115.qrcode_token()
    except P115Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/p115/qrcode/status")
def p115_qrcode_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    uid, time_, sign = q(request, "uid"), q(request, "time"), q(request, "sign")
    if not (uid and time_ and sign):
        raise HTTPException(status_code=400, detail="缺少 uid、time 或 sign")
    try:
        return state(request).p115.qrcode_status(uid, time_, sign)
    except P115Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/p115/cookies")
async def p115_set_cookies(request: Request, ctx: AuthContext = Depends(require_admin)):
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        body = {}
    cookies = (body.get("cookies") or "").strip()
    if not cookies:
        raise HTTPException(status_code=400, detail="cookies 不可為空")
    svc = state(request).p115
    svc.set_cookies(cookies)
    return {"logged_in": True, "user": svc.user_info()}


@router.post("/p115/logout")
def p115_logout(request: Request, ctx: AuthContext = Depends(require_admin)):
    state(request).p115.logout()
    return Response(status_code=204)


def _tasks_view(st) -> list:
    libs = [Path(p).expanduser().resolve() for lib in st.config.libraries for p in lib.paths]

    def in_library(local: str) -> bool:
        path = Path(local).expanduser().resolve()
        return any(path == lib or lib in path.parents or path in lib.parents for lib in libs)

    return [
        {"remote": t.remote, "local": t.local, "in_library": in_library(t.local), "state": state_}
        for t, state_ in zip(st.strm_sync.tasks, st.strm_sync.task_states())
    ]


@router.post("/p115/strm/sync")
def p115_strm_sync(request: Request, ctx: AuthContext = Depends(require_admin)):
    st = state(request)
    if not st.p115.logged_in:
        raise HTTPException(status_code=400, detail="尚未登入 115")
    if not st.strm_sync.tasks:
        raise HTTPException(status_code=400, detail="還沒有同步任務，請先新增「115 目錄 → 本機資料夾」")
    st.strm_sync.remember_base_url(str(request.base_url))
    mode = INCREMENTAL if (q(request, "mode") or "").lower().startswith("inc") else FULL
    started = st.strm_sync.run_in_background(mode)
    return {"started": started, "result": st.strm_sync.result.as_dict()}


@router.get("/p115/strm/status")
def p115_strm_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    st = state(request)
    return {
        "tasks": _tasks_view(st),
        "libraries": [{"name": lib.name, "type": lib.type, "paths": lib.paths} for lib in st.config.libraries],
        "base_url": st.strm_sync.base_url,
        "result": st.strm_sync.result.as_dict(),
    }


@router.put("/p115/strm/tasks")
async def p115_strm_tasks(request: Request, ctx: AuthContext = Depends(require_admin)):
    try:
        body = json.loads(await request.body() or b"[]")
    except ValueError:
        raise HTTPException(status_code=400, detail="格式錯誤")
    tasks = []
    for t in body if isinstance(body, list) else []:
        remote = str(t.get("remote") or "").strip()
        local = str(t.get("local") or "").strip()
        if not (remote and local):
            raise HTTPException(status_code=400, detail="115 目錄和本機資料夾都要填")
        if not remote.startswith("/"):
            remote = "/" + remote
        tasks.append(StrmTask(remote=remote, local=local))
    st = state(request)
    st.strm_sync.set_tasks(tasks)
    return {"tasks": _tasks_view(st)}


def _redirect(request: Request, pickcode: str) -> Response:
    if not PICKCODE_RE.match(pickcode):
        raise HTTPException(status_code=400, detail=f"Bad pickcode: {pickcode}")
    try:
        url = state(request).p115.download_url(pickcode, request.headers.get("user-agent", ""))
    except P115Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return RedirectResponse(url=url, status_code=302)


# 本伺服器產生的 strm：/d/{pickcode}.mkv（可再帶 ?/原檔名 或 /原檔名，會被忽略）
@router.api_route("/d/{code}", methods=["GET", "HEAD"])
@router.api_route("/d/{code}/{name:path}", methods=["GET", "HEAD"])
def p115_short_link(code: str, request: Request):
    return _redirect(request, code.split(".", 1)[0])


# 相容其他工具產生的 strm（例如 P115StrmHelper），換個主機即可沿用
@router.api_route("/p115/redirect", methods=["GET", "HEAD"])
@router.api_route("/api/v1/plugin/p115strmhelper/redirect_url", methods=["GET", "HEAD"])
def p115_redirect(request: Request):
    return _redirect(request, q(request, "pickcode") or q(request, "pick_code") or "")
