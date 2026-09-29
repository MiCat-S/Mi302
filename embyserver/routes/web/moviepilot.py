"""MoviePilot：測試連線、刮削、補全缺集。"""

from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ...auth import AuthContext, require_admin
from ...moviepilot import library_series
from ..common import q, q_int, state
from .common import json_body

router = APIRouter()


# ---------------- 刮削 ----------------


@router.post("/web/api/moviepilot/test")
def moviepilot_test(request: Request, ctx: AuthContext = Depends(require_admin)):
    return state(request).moviepilot.test()


@router.get("/web/api/moviepilot/status")
def moviepilot_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    mp = state(request).moviepilot
    return {
        "enabled": mp.enabled, "can_subscribe": mp.can_subscribe,
        "result": mp.result.as_dict(), "fill": mp.fill_result.as_dict(),
    }


@router.post("/web/api/moviepilot/scrape")
def moviepilot_scrape(request: Request, ctx: AuthContext = Depends(require_admin)):
    """把媒體庫裡還沒有 nfo 的影片都送去 MoviePilot 刮削。"""
    mp = state(request).moviepilot
    if not mp.enabled:
        raise HTTPException(status_code=400, detail="請先填好 MoviePilot 網址與 API 令牌並儲存")
    started = mp.scrape_in_background(None, "manual")
    return {"started": started, "result": mp.result.as_dict()}


# ---------------- 補全缺集 ----------------


@router.get("/web/api/series")
def list_series(request: Request, ctx: AuthContext = Depends(require_admin)):
    """媒體庫裡的劇和每一季的集數、集號空洞；q 搜尋劇名，year 只列那一年的，gaps=1 只列有空洞的，offset、limit 分頁。

    years 是媒體庫裡所有劇的年份（不受篩選影響），給網頁的年份下拉選單用。
    """
    db = state(request).db
    offset = max(q_int(request, "offset", 0) or 0, 0)
    limit = min(max(q_int(request, "limit", 20) or 20, 1), 500)
    items, total = library_series(
        db, q(request, "q") or "", q(request, "gaps") in ("1", "true"), limit=limit, offset=offset,
        year=q_int(request, "year"),
    )
    years = [r["year"] for r in db.query(
        "SELECT DISTINCT year FROM items WHERE type='Series' AND year IS NOT NULL ORDER BY year DESC")]
    return {"items": items, "total": total, "offset": offset, "more": offset + len(items) < total, "years": years}


@router.post("/web/api/moviepilot/fill")
async def moviepilot_fill(request: Request, ctx: AuthContext = Depends(require_admin)):
    """補全缺集：{"series": [id, ...]} 只送這些劇；空的就送所有有 tmdbid 的劇。"""
    st = state(request)
    mp = st.moviepilot
    body = await json_body(request)
    if not mp.enabled:
        raise HTTPException(status_code=400, detail="請先填好 MoviePilot 網址與 API 令牌並儲存")
    if not mp.can_subscribe:
        raise HTTPException(status_code=400, detail="建訂閱的 API 只接受帳號登入，請在「MoviePilot 帳號密碼」填好再儲存")
    ids = body.get("series")
    wanted = {int(i) for i in ids if str(i).isdigit()} if isinstance(ids, list) and ids else None
    all_shows = (await run_in_threadpool(library_series, st.db))[0]
    shows = [s for s in all_shows if (s["id"] in wanted if wanted is not None else bool(s["tmdbid"]))]
    if not shows:
        raise HTTPException(status_code=400, detail="沒有可以送的劇：要先刮削過、有 tmdbid")
    started = mp.fill_in_background(shows, "manual")
    return {"started": started, "result": mp.fill_result.as_dict()}
