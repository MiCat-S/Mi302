"""MoviePilot：測試連線、刮削、補全缺集、交給 MoviePilot 整理集號不對的劇。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ...auth import AuthContext, require_admin
from ...moviepilot import library_series
from ...reorganize import ReorgError, candidates as reorg_candidates
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


# ---------------- 交給 MoviePilot 整理集號不對的劇 ----------------


@router.get("/web/api/moviepilot/reorganize")
def reorganize_list(request: Request, ctx: AuthContext = Depends(require_admin)):
    """集號從檔名猜的、或認不出來的集，一季一列；附上目前的整理工作和缺什麼設定。"""
    st = state(request)
    offset, limit = max(q_int(request, "offset") or 0, 0), min(max(q_int(request, "limit") or 20, 1), 200)
    items, total = reorg_candidates(st.db, q(request, "q") or "", offset, limit)
    return {"items": items, "total": total, "job": st.reorganizer.job.as_dict(), "ready": st.reorganizer.ready()}


@router.get("/web/api/moviepilot/reorganize/plan")
def reorganize_plan(request: Request, ctx: AuthContext = Depends(require_admin)):
    """一季要送哪些 115 檔案、分成幾批、每批的集數定位模板。會列 115 的資料夾，可能要幾秒。"""
    st = state(request)
    try:
        return st.reorganizer.plan(q_int(request, "series") or 0, q_int(request, "season") or 0)
    except ReorgError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/web/api/moviepilot/reorganize/folder")
def reorganize_folder_plan(request: Request, ctx: AuthContext = Depends(require_admin)):
    """「瀏覽 115」裡的一個資料夾：裡面（含子資料夾）的影片、建議的類型、TMDB 編號、季、分批。"""
    st = state(request)
    try:
        return st.reorganizer.folder_plan(q_int(request, "cid") or 0, q(request, "path") or "")
    except ReorgError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/web/api/moviepilot/reorganize/preview")
async def reorganize_preview(request: Request, ctx: AuthContext = Depends(require_admin)):
    """請 MoviePilot 只算不做：{plan_id（或 series_id + season）, tmdbid, type: auto|tv|movie, season,
    target: auto|parent|path, target_path, scrape, groups: [{key, template, enabled}], expect_dir}。"""
    st = state(request)
    body = await json_body(request)
    if not st.reorganizer.ready()["login"]:
        raise HTTPException(status_code=400, detail="MoviePilot 的手動整理只接受帳號登入，請在「MoviePilot 帳號密碼」填好再儲存")
    plan_id = str(body.get("plan_id") or "") or f"s{int(body.get('series_id') or 0)}-{int(body.get('season') or 0)}"
    season = body.get("season")
    try:
        return await run_in_threadpool(
            st.reorganizer.preview, plan_id, str(body.get("tmdbid") or ""), str(body.get("type") or "auto"),
            int(season) if str(season if season is not None else "").strip().isdigit() else None,
            str(body.get("target") or "auto"), str(body.get("target_path") or ""), bool(body.get("scrape", True)),
            [g for g in body.get("groups") or [] if isinstance(g, dict)], str(body.get("expect_dir") or ""),
        )
    except ReorgError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/web/api/moviepilot/reorganize/execute")
async def reorganize_execute(request: Request, ctx: AuthContext = Depends(require_admin)):
    """照預覽執行：{"token": 預覽代碼} 或 {"tokens": [...], "cleanup": [{cid, path}]}（合併重複的資料夾：幾個預覽一起，
    整理完沒有影片留下的來源資料夾移到 115 回收站）。只送預覽成功的檔案，在背景跑，進度看 GET /web/api/moviepilot/reorganize。"""
    st = state(request)
    body = await json_body(request)
    tokens = body.get("tokens") if isinstance(body.get("tokens"), list) else [body.get("token")]
    cleanup = [c for c in body.get("cleanup") or [] if isinstance(c, dict) and str(c.get("cid") or "").isdigit()]
    try:
        st.reorganizer.execute_in_background([str(t or "") for t in tokens], cleanup)
    except ReorgError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"job": st.reorganizer.job.as_dict()}
