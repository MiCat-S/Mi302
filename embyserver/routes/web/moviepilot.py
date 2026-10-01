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


@router.post("/web/api/moviepilot/stop")
async def moviepilot_stop(request: Request, ctx: AuthContext = Depends(require_admin)):
    """停止刮削或補全缺集：{"what": "scrape" | "fill"}。刮削送出去的做完、沒送的不送；補全做完手上這一季就停。"""
    body = await json_body(request)
    what = str(body.get("what") or "")
    if what not in ("scrape", "fill"):
        raise HTTPException(status_code=400, detail="what 要是 scrape 或 fill")
    return {"stopped": state(request).moviepilot.cancel(what)}


# ---------------- 補全缺集 ----------------


@router.get("/web/api/series")
def list_series(request: Request, ctx: AuthContext = Depends(require_admin)):
    """媒體庫裡的劇和每一季的集數、集號空洞、對照 TMDB 缺哪幾集（還沒對照過的季 missing 是 null）；q 搜尋劇名，
    year 只列那一年的，missing=1 只列對照 TMDB 真的缺集的，gaps=1 只列集號有空洞的，excluded=1 只列標了「不補」的，
    offset、limit 分頁。每一部附上 excluded（標了「不補」）。

    years 是媒體庫裡所有劇的年份、stats 是整個媒體庫缺集的、還沒對照的、對照過的各幾部（都不受篩選影響）。
    """
    st = state(request)
    db = st.db
    offset = max(q_int(request, "offset", 0) or 0, 0)
    limit = min(max(q_int(request, "limit", 20) or 20, 1), 500)
    excluded = st.moviepilot.fill_excluded()
    stats: dict = {}
    items, total = library_series(
        db, q(request, "q") or "", q(request, "gaps") in ("1", "true"), limit=limit, offset=offset,
        year=q_int(request, "year"), tmdbids=set(excluded) if q(request, "excluded") in ("1", "true") else None,
        missing_only=q(request, "missing") in ("1", "true"), stats=stats,
    )
    for s in items:
        s["excluded"] = str(s["tmdbid"]) in excluded if s["tmdbid"] else False
    years = [r["year"] for r in db.query(
        "SELECT DISTINCT year FROM items WHERE type='Series' AND year IS NOT NULL ORDER BY year DESC")]
    return {"items": items, "total": total, "offset": offset, "more": offset + len(items) < total, "years": years,
            "excluded": len(excluded), "stats": stats}


@router.post("/web/api/moviepilot/fill/exclude")
async def moviepilot_fill_exclude(request: Request, ctx: AuthContext = Depends(require_admin)):
    """補全缺集時跳過這部劇：{"tmdbid": 123, "name": "劇名", "exclude": true}；exclude=false 取消。照 tmdbid 記。"""
    body = await json_body(request)
    tmdbid = str(body.get("tmdbid") or "")
    if not tmdbid.isdecimal():
        raise HTTPException(status_code=400, detail="要有 tmdbid（沒有 tmdbid 的劇本來就不會補）")
    excluded = state(request).moviepilot.set_fill_excluded(int(tmdbid), str(body.get("name") or ""),
                                                           bool(body.get("exclude", True)))
    return {"excluded": tmdbid in excluded, "count": len(excluded)}


@router.post("/web/api/moviepilot/fill")
async def moviepilot_fill(request: Request, ctx: AuthContext = Depends(require_admin)):
    """補全缺集：{"series": [id, ...]} 只送這些劇；空的就送所有有 tmdbid 的劇。
    {"check": true}：只對照 TMDB、記下每一季缺哪幾集（清單就看得出誰真的缺），不建訂閱。"""
    st = state(request)
    mp = st.moviepilot
    body = await json_body(request)
    check = bool(body.get("check"))
    if not mp.enabled:
        raise HTTPException(status_code=400, detail="請先填好 MoviePilot 網址與 API 令牌並儲存")
    if not check and not mp.can_subscribe:
        raise HTTPException(status_code=400, detail="建訂閱的 API 只接受帳號登入，請在「MoviePilot 帳號密碼」填好再儲存")
    ids = body.get("series")
    wanted = {int(i) for i in ids if str(i).isdecimal()} if isinstance(ids, list) and ids else None
    all_shows = (await run_in_threadpool(library_series, st.db))[0]
    shows = [s for s in all_shows if (s["id"] in wanted if wanted is not None else bool(s["tmdbid"]))]
    if not shows:
        raise HTTPException(status_code=400, detail="沒有可以送的劇：要先刮削過、有 tmdbid")
    started = mp.fill_in_background(shows, "manual", check) if check else mp.fill_in_background(shows, "manual")
    return {"started": started, "result": mp.fill_result.as_dict()}
