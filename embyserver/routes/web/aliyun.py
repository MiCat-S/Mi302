"""阿里雲盤：帳號狀態、貼 refresh token 登入、登出、選資料夾（從阿里雲盤秒傳到 115 用）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ...aliyun import AliyunError
from ...auth import AuthContext, require_admin
from ..common import q, state
from .common import json_body

router = APIRouter()


def _aliyun(fn, *args):
    try:
        return fn(*args)
    except AliyunError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/web/api/aliyun/status")
def aliyun_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    """{logged_in, name, drives（資源庫、備份盤裡有的）, own_client（用自己的 client id 換 token）, error}。"""
    return state(request).aliyun.status()


@router.post("/web/api/aliyun/token")
async def aliyun_token(request: Request, ctx: AuthContext = Depends(require_admin)):
    """登入：{refresh_token}。存起來、換一次 access token、讀帳號資訊；不成功回 400，換回原本的 refresh token。
    沒填自己的 client id 時，會把 refresh token 送給設定裡的線上 API（OpenList 的服務）換 access token。"""
    body = await json_body(request)
    return await run_in_threadpool(_aliyun, state(request).aliyun.login, str(body.get("refresh_token") or ""))


@router.post("/web/api/aliyun/logout")
def aliyun_logout(request: Request, ctx: AuthContext = Depends(require_admin)):
    st = state(request)
    st.aliyun.logout()
    return st.aliyun.status()


@router.get("/web/api/aliyun/dirs")
def aliyun_dirs(request: Request, ctx: AuthContext = Depends(require_admin)):
    """選資料夾的對話框：path 這一層的子資料夾和檔案數；path 空的或 / 時是「資源庫」「備份盤」。"""
    return _aliyun(state(request).aliyun.dirs, q(request, "path") or "/")
