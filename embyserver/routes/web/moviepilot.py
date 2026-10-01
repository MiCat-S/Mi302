"""MoviePilot：測試連線、刮削、補全缺集。"""

from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ...auth import AuthContext, require_admin
from ...moviepilot import MoviePilotError, library_series
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
        "result": mp.result.as_dict(), "fill": mp.fill_result.as_dict(), "unsubscribe": mp.unsubscribe_result.as_dict(),
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
    """停止刮削、補全缺集或取消訂閱：{"what": "scrape" | "fill" | "unsubscribe"}。刮削送出去的做完、沒送的不送；
    補全做完手上這一季就停；取消訂閱刪掉的就刪掉了，剩下的留著。"""
    body = await json_body(request)
    what = str(body.get("what") or "")
    if what not in ("scrape", "fill", "unsubscribe"):
        raise HTTPException(status_code=400, detail="what 要是 scrape、fill 或 unsubscribe")
    return {"stopped": state(request).moviepilot.cancel(what)}


# ---------------- 補全缺集 ----------------


@router.get("/web/api/series")
def list_series(request: Request, ctx: AuthContext = Depends(require_admin)):
    """補全缺集的清單：媒體庫裡的劇、每一季的集數、對照 TMDB 缺哪幾集（還沒對照過的季 missing 是 null）、
    缺集的季 MoviePilot 訂閱了沒（subscribed；讀不到訂閱時是 null）。每一部有 state：missing（缺集、還沒訂閱）、
    subscribed（缺集的季都訂閱了）、unchecked（還沒對照）、notmdb（沒有 tmdbid）、complete（齊全）、excluded（標了「不補」）。

    view 只列那一種（missing、subscribed、unchecked（含 notmdb）、excluded，不給是全部）；q 搜尋劇名，year 只列那一年的，
    gaps=1 只列集號有空洞的，offset、limit 分頁。
    years 是媒體庫裡所有劇的年份、stats 是整個媒體庫各種狀態幾部（都不受篩選影響）、subscriptions 是 MoviePilot
    現在有幾個訂閱（讀不到是 null）。
    """
    st = state(request)
    db, mp = st.db, st.moviepilot
    offset = max(q_int(request, "offset", 0) or 0, 0)
    limit = min(max(q_int(request, "limit", 20) or 20, 1), 500)
    excluded = mp.fill_excluded()
    view = q(request, "view") or ("excluded" if q(request, "excluded") in ("1", "true") else "")
    subs = mp.known_subscriptions()
    stats: dict = {}
    items, total = library_series(
        db, q(request, "q") or "", q(request, "gaps") in ("1", "true"), limit=limit, offset=offset,
        year=q_int(request, "year"), view=view, excluded=set(excluded), subscribed=mp.subscribed_seasons(), stats=stats,
    )
    for s in items:
        s["excluded"] = s["state"] == "excluded"
    years = [r["year"] for r in db.query(
        "SELECT DISTINCT year FROM items WHERE type='Series' AND year IS NOT NULL ORDER BY year DESC")]
    return {"items": items, "total": total, "offset": offset, "more": offset + len(items) < total, "years": years,
            "excluded": len(excluded), "stats": stats, "subscriptions": len(subs) if subs is not None else None}


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
    """補全缺集：{"series": [id, ...]} 只送這些劇；{"view": "missing"} 送清單上缺集、又還沒訂閱的；都沒給就送所有有
    tmdbid 的劇。{"check": true}：只對照 TMDB、記下每一季缺哪幾集，不建訂閱（{"view": "unchecked"} 只對照還沒對照過的）。"""
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
    view = str(body.get("view") or "")

    def pick() -> list:
        shows = library_series(st.db, excluded=set(mp.fill_excluded()), subscribed=mp.subscribed_seasons())[0]
        if wanted is not None:
            return [s for s in shows if s["id"] in wanted]
        if view == "missing":
            return [s for s in shows if s["state"] == "missing"]
        if view == "unchecked":
            return [s for s in shows if s["state"] == "unchecked"]
        return [s for s in shows if s["tmdbid"]]

    shows = await run_in_threadpool(pick)
    if not shows:
        raise HTTPException(status_code=400, detail="沒有可以送的劇" + ("" if view else "：要先刮削過、有 tmdbid"))
    started = mp.fill_in_background(shows, "manual", check) if check else mp.fill_in_background(shows, "manual")
    return {"started": started, "count": len(shows), "result": mp.fill_result.as_dict()}


# ---------------- 取消訂閱 ----------------

UNSUBSCRIBE_WORD = "取消訂閱"


def _need_login(mp) -> None:
    if not mp.enabled:
        raise HTTPException(status_code=400, detail="請先填好 MoviePilot 網址與 API 令牌並儲存")
    if not mp.can_subscribe:
        raise HTTPException(status_code=400, detail="訂閱的 API 只接受帳號登入，請在「MoviePilot 帳號密碼」填好再儲存")


@router.get("/web/api/moviepilot/subscriptions")
def moviepilot_subscriptions(request: Request, ctx: AuthContext = Depends(require_admin)):
    """MoviePilot 裡現在有幾個訂閱：{total, tv, other}（給「取消所有訂閱」的確認框）。"""
    mp = state(request).moviepilot
    _need_login(mp)
    try:
        return mp.subscription_counts()
    except MoviePilotError as exc:
        raise HTTPException(status_code=502, detail=f"讀不到 MoviePilot 的訂閱：{exc}")


@router.post("/web/api/moviepilot/subscriptions/clear")
async def moviepilot_subscriptions_clear(request: Request, ctx: AuthContext = Depends(require_admin)):
    """取消 MoviePilot 裡所有的訂閱：{"confirm": "取消訂閱"}，沒帶 confirm 不做。在背景一個一個刪，進度看
    GET /web/api/moviepilot/status 的 unsubscribe。只刪訂閱，下載好、整理好的檔案不動。"""
    body = await json_body(request)
    if body.get("confirm") != UNSUBSCRIBE_WORD:
        raise HTTPException(status_code=400, detail=f"要在 confirm 帶上「{UNSUBSCRIBE_WORD}」才會取消訂閱")
    mp = state(request).moviepilot
    _need_login(mp)
    started = mp.unsubscribe_all_in_background()
    return {"started": started, "unsubscribe": mp.unsubscribe_result.as_dict()}
