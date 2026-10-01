"""伺服器：目前的版本、檢查更新、更新到最新版、重新啟動。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ...auth import AuthContext, require_admin
from ...updater import UpdateError
from ..common import state

router = APIRouter()


def _busy(request: Request) -> list:
    """正在進行、重新啟動會中斷的背景工作（網頁確認時列出來）。"""
    st = state(request)
    jobs = [
        ("115 同步", st.strm_sync.result.running), ("媒體庫掃描", st.scanner.scanning),
        ("MoviePilot 刮削", st.moviepilot.result.running), ("補全缺集", st.moviepilot.fill_result.running),
        ("MoviePilot 整理", st.reorganizer.job.running), ("媒體資訊提取", st.prober.result.running),
        ("重複檔案", st.dupes.job.running), ("空資料夾", st.empty_dirs.job.running),
    ]
    return [name for name, running in jobs if running]


def _status(request: Request) -> dict:
    return {**state(request).updater.status(), "busy": _busy(request)}


@router.get("/web/api/server")
def server_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    """版本、上次檢查更新的結果、更新進度；boot 每次啟動都不同，網頁用它看出重新啟動完成了。"""
    return _status(request)


@router.post("/web/api/server/check")
async def server_check(request: Request, ctx: AuthContext = Depends(require_admin)):
    """現在就向 GitHub 查有沒有新版（git fetch，最多一分半）。"""
    await run_in_threadpool(state(request).updater.check)
    return _status(request)


@router.post("/web/api/server/update")
def server_update(request: Request, ctx: AuthContext = Depends(require_admin)):
    """更新到最新版，在背景跑：下載、需要時裝相依套件、檢查新版能啟動，再重新啟動；進度看 GET /web/api/server。"""
    try:
        state(request).updater.update_in_background()
    except UpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _status(request)


@router.post("/web/api/server/restart")
def server_restart(request: Request, ctx: AuthContext = Depends(require_admin)):
    """重新啟動 Mi302（一秒後）。網頁接著輪詢 GET /web/api/server，boot 變了就是回來了。"""
    try:
        state(request).updater.restart()
    except UpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _status(request)
