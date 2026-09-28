"""媒體庫掃描、從資料夾批量新增媒體庫的建議。"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ... import library_suggest
from ...auth import AuthContext, require_admin
from ...dto import image_tag
from ..common import q, state
from .common import json_body

router = APIRouter()


@router.get("/web/api/scan")
def scan_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    st = state(request)
    rows = st.db.query(
        "SELECT l.name, COUNT(i.id) AS c FROM items l LEFT JOIN items i "
        "ON i.library_id=l.id AND i.type IN ('Movie','Series','Episode') "
        "WHERE l.type='CollectionFolder' GROUP BY l.id"
    )
    counts = {r["name"]: r["c"] for r in rows}
    items = {
        r["name"]: r for r in st.db.query("SELECT id, name, primary_image FROM items WHERE type='CollectionFolder'")
    }
    libs = [
        {
            "name": lib.name,
            "count": counts.get(lib.name, 0),
            "id": str(items[lib.name]["id"]) if lib.name in items else None,
            "cover": image_tag(items[lib.name]["primary_image"]) if lib.name in items else None,
            "custom_cover": bool(lib.name in items and st.scanner.custom_image(items[lib.name]["id"], "primary_image")),
            "missing": [p for p in lib.paths if not Path(p).expanduser().is_dir()],
        }
        for lib in st.config.libraries
    ]
    return {
        "scanning": st.scanner.scanning, "current": st.scanner.current, "libraries": libs,
        # 進度：total 是上次掃描後的項目數，第一次掃描是 0（網頁顯示忙碌條）
        "progress": {"done": st.scanner.touched, "total": st.scanner.expected, "item": st.scanner.item},
    }


@router.post("/web/api/scan")
async def scan_now(request: Request, ctx: AuthContext = Depends(require_admin)):
    """重新掃描：{"library": 名稱} 只掃一個媒體庫，{"path": 路徑} 只掃一個資料夾或檔案，都沒有就全部掃。"""
    st = state(request)
    body = await json_body(request)
    scanner = st.scanner
    if body.get("library"):
        name = str(body["library"])
        if name not in {lib.name for lib in st.config.libraries}:
            raise HTTPException(status_code=400, detail=f"找不到媒體庫「{name}」，新加的媒體庫要先儲存")
        job, args = scanner.scan_libraries, ([name],)
    elif body.get("path"):
        path = str(body["path"]).strip()
        if not scanner.in_library(path):
            raise HTTPException(status_code=400, detail="這個位置不在任何媒體庫的資料夾裡")
        job, args = scanner.scan_paths, ([path],)
    else:
        job, args = scanner.scan_all, ()
    threading.Thread(target=job, args=args, daemon=True).start()
    return Response(status_code=204)


@router.get("/web/api/libraries/suggest")
def suggest_libraries(request: Request, ctx: AuthContext = Depends(require_admin)):
    """批量新增媒體庫：列出某個資料夾底下的子資料夾，猜每個是電影還是劇集。"""
    st = state(request)
    try:
        return library_suggest.suggest(q(request, "path") or "", st.config.libraries)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
