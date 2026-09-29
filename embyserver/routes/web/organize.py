"""整理 115 網盤：清單、問 MoviePilot 檢查、預覽、執行、集數定位推薦、瀏覽 115 裡挑的資料夾、刪除、整理工作的進度。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ...auth import AuthContext, require_admin
from ...organize115 import OrganizeError
from ...reorganize import ReorgError
from ..common import q, q_int, state
from .common import json_body

router = APIRouter()

LOGIN_NEEDED = "MoviePilot 的手動整理只接受帳號登入，請在「MoviePilot 帳號密碼」填好再儲存"


def _run(fn, *args):
    try:
        return fn(*args)
    except (OrganizeError, ReorgError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/web/api/115/organize")
def organize_list(request: Request, ctx: AuthContext = Depends(require_admin)):
    """要整理的資料夾：問過 MoviePilot 名稱不一樣的、集號不對的（q 搜尋、kind=series|movie|episodes、offset、limit）；
    瀏覽 115 加進來的在 pinned。附上背景檢查的進度（job）、還沒問過的數量（unchecked）、缺什麼設定（ready）、
    目前的整理工作（reorg_job）。不向 MoviePilot 請求。"""
    st = state(request)
    offset, limit = max(q_int(request, "offset") or 0, 0), min(max(q_int(request, "limit") or 50, 1), 200)
    result = st.organizer.list(q(request, "q") or "", q(request, "kind") or "", offset, limit)
    return {**result, "offset": offset, "ready": st.reorganizer.ready(), "reorg_job": st.reorganizer.job.as_dict()}


@router.get("/web/api/115/organize/job")
def organize_job(request: Request, ctx: AuthContext = Depends(require_admin)):
    """目前（或上一次）的整理工作。"""
    st = state(request)
    return {"job": st.reorganizer.job.as_dict(), "ready": st.reorganizer.ready()}


@router.post("/web/api/115/organize/check")
async def organize_check(request: Request, ctx: AuthContext = Depends(require_admin)):
    """在背景問 MoviePilot 每個資料夾整理後叫什麼：{"refresh": true} 連問過的也重問。進度看 GET /web/api/115/organize。"""
    st = state(request)
    body = await json_body(request)
    started = _run(st.organizer.check_in_background, bool(body.get("refresh")))
    return {"started": started, "job": st.organizer.job.as_dict()}


@router.post("/web/api/115/organize/folder")
async def organize_folder(request: Request, ctx: AuthContext = Depends(require_admin)):
    """瀏覽 115 裡挑的資料夾：{cid, path}。列一次目錄、問 MoviePilot 叫什麼，釘在清單最上面；回傳它。"""
    st = state(request)
    body = await json_body(request)
    return await run_in_threadpool(_run, st.organizer.folder_unit, int(body.get("cid") or 0), str(body.get("path") or ""))


@router.delete("/web/api/115/organize/folder/{unit_id}")
def organize_unpin(unit_id: str, request: Request, ctx: AuthContext = Depends(require_admin)):
    state(request).organizer.unpin(unit_id)
    return {"ok": True}


@router.post("/web/api/115/organize/preview")
async def organize_preview(request: Request, ctx: AuthContext = Depends(require_admin)):
    """請 MoviePilot 只算不做：{id, parts: {部分: {type: tv|movie, tmdbid, season, format}}, target: parent|auto|path,
    target_path, scrape}，沒指定的讓 MoviePilot 自己認。回傳每個檔案的新位置、它認成什麼和 Mi302 的檢查；
    有能整理的就給預覽代碼 token，用 POST /web/api/115/organize/execute 執行。"""
    st = state(request)
    body = await json_body(request)
    if not st.reorganizer.ready()["login"]:
        raise HTTPException(status_code=400, detail=LOGIN_NEEDED)
    parts = body.get("parts") if isinstance(body.get("parts"), dict) else {}
    overrides = {str(k): v for k, v in parts.items() if isinstance(v, dict)}
    return await run_in_threadpool(_run, st.organizer.preview, str(body.get("id") or ""), overrides,
                                   str(body.get("target") or ""), str(body.get("target_path") or ""), bool(body.get("scrape", True)))


@router.post("/web/api/115/organize/recommend")
async def organize_recommend(request: Request, ctx: AuthContext = Depends(require_admin)):
    """一部分的集數定位模板：{id, part}。先請 MoviePilot 推薦，推薦不出來再用 Mi302 從檔名看的（source 標明）。"""
    st = state(request)
    body = await json_body(request)
    return await run_in_threadpool(_run, st.organizer.recommend, str(body.get("id") or ""), str(body.get("part") or ""))


@router.post("/web/api/115/organize/execute")
async def organize_execute(request: Request, ctx: AuthContext = Depends(require_admin)):
    """照預覽執行：{"tokens": [...], "cleanup": [{cid, path}]}；整理完沒有影片留下的來源資料夾移到 115 回收站。
    在背景跑，進度看 GET /web/api/115/organize/job。"""
    st = state(request)
    body = await json_body(request)
    tokens = body.get("tokens") if isinstance(body.get("tokens"), list) else [body.get("token")]
    cleanup = [c for c in body.get("cleanup") or [] if isinstance(c, dict) and str(c.get("cid") or "").isdigit()]
    _run(st.reorganizer.execute_in_background, [str(t or "") for t in tokens], cleanup)
    return {"job": st.reorganizer.job.as_dict()}


@router.get("/web/api/115/organize/episodes")
def organize_episodes(request: Request, ctx: AuthContext = Depends(require_admin)):
    """一部劇在媒體庫裡的每一集（給刪除對話框勾），集號不對的標 problem；附上劇集資料夾。只看資料庫。"""
    st = state(request)
    return _run(st.reorganizer.series_files, q_int(request, "series") or 0, q_int(request, "season"))


@router.post("/web/api/115/organize/delete")
async def organize_delete(request: Request, ctx: AuthContext = Depends(require_admin)):
    """刪除，都是送進 115 回收站（可以還原），本機 strm、nfo 和媒體庫跟著拿掉。要用掃碼登入 115。
    {id}：清單上的一個整個刪掉（劇集或電影資料夾、沒有自己資料夾的電影、瀏覽 115 加進來的資料夾）；
    {series_id, file_ids, remove_folder}：刪劇的這幾集，劇集資料夾刪空了可以一起移走。"""
    st = state(request)
    body = await json_body(request)
    if body.get("id"):
        return await run_in_threadpool(_run, st.organizer.delete, str(body["id"]))
    ids = [int(i) for i in body.get("file_ids") or [] if str(i).isdigit()]
    try:
        series_id = int(body.get("series_id") or 0)
    except ValueError:
        raise HTTPException(status_code=400, detail="series_id 格式錯誤")
    return await run_in_threadpool(_run, st.reorganizer.delete_episodes, series_id, ids, bool(body.get("remove_folder")))
