"""qBittorrent：測試連線、沒速度的種子和刪除紀錄、馬上看一次。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ...auth import AuthContext, require_admin
from ..common import state

router = APIRouter()


@router.post("/web/api/qbittorrent/test")
def qbittorrent_test(request: Request, ctx: AuthContext = Depends(require_admin)):
    return state(request).qbittorrent.test()


@router.get("/web/api/qbittorrent/status")
def qbittorrent_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    """上一輪看到的（下載中、排在後面的各幾個、沒速度的種子、錯誤）和最近刪掉的；不打 qBittorrent。"""
    return state(request).qbittorrent.status()


@router.post("/web/api/qbittorrent/check")
def qbittorrent_check(request: Request, ctx: AuthContext = Depends(require_admin)):
    """馬上看一次。開了自動刪除時，沒速度夠久的照樣會刪。"""
    qb = state(request).qbittorrent
    if not qb.enabled:
        raise HTTPException(status_code=400, detail="請先填好 qBittorrent 網址並儲存")
    qb.check()
    return qb.status()
