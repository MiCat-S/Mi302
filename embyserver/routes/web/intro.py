"""片頭片尾：學到的狀態、清除、每一季的清單和手動設定。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ...auth import AuthContext, require_admin
from ..common import q, q_int, state
from .common import json_body

router = APIRouter()


@router.get("/web/api/intro/status")
def intro_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    """片頭片尾：學到幾季、最近學到的。"""
    return state(request).intro.status()


@router.post("/web/api/intro/clear")
def intro_clear(request: Request, ctx: AuthContext = Depends(require_admin), body: dict = Depends(json_body)):
    """清掉學到的片頭片尾：{"season_id": id} 只清一季，{"all": true} 全清。

    兩個都沒給、season_id 不是正整數或不是一季都回錯誤：輸入有錯不能變成清掉全部。"""
    sid = body.get("season_id")
    st = state(request)
    if sid is None:
        if body.get("all") is not True:
            raise HTTPException(status_code=400, detail="要指定 season_id，或送 all: true 清掉全部")
        return {"removed": st.intro.clear(None)}
    if isinstance(sid, bool) or not isinstance(sid, (int, str)) or not str(sid).isdecimal() or int(sid) <= 0:
        raise HTTPException(status_code=400, detail="season_id 要是正整數")
    row = st.db.get_item(int(sid))
    if row is None or row["type"] != "Season":
        raise HTTPException(status_code=404, detail="找不到這一季")
    return {"removed": st.intro.clear(int(sid))}


@router.get("/web/api/intro/seasons")
def intro_seasons(request: Request, ctx: AuthContext = Depends(require_admin)):
    """片頭片尾的季清單：q 搜尋劇名（沒學到的季也列出來），offset、limit 分頁。"""
    offset = max(q_int(request, "offset", 0) or 0, 0)
    limit = min(max(q_int(request, "limit", 20) or 20, 1), 100)
    return state(request).intro.seasons(q(request, "q") or "", limit, offset)


@router.put("/web/api/intro/seasons/{season_id}")
async def intro_set_season(season_id: int, request: Request, ctx: AuthContext = Depends(require_admin)):
    """手動設定一季的片頭片尾（秒）：{"intro": {"mode", "start", "end"}, "credits": {"mode", "tail"}, "all_seasons"}。

    mode 是 auto（照學的）、manual（用這裡的值）或 none（這一季沒有）；兩個都 auto 就是取消手動設定。
    all_seasons 為真時，同一部劇的每一季都用這個設定。
    """
    body = await json_body(request)
    intro, credits = body.get("intro") or {}, body.get("credits") or {}
    if not isinstance(intro, dict) or not isinstance(credits, dict):
        raise HTTPException(status_code=400, detail="格式錯誤")
    learner = state(request).intro

    def apply():
        targets = learner.sibling_seasons(season_id)
        if season_id not in targets:
            raise HTTPException(status_code=404, detail="找不到這一季")
        for sid in targets if body.get("all_seasons") else [season_id]:
            learner.set_manual(sid, str(intro.get("mode") or "auto"), intro.get("start"), intro.get("end"),
                               str(credits.get("mode") or "auto"), credits.get("tail"))
        return len(targets) if body.get("all_seasons") else 1

    try:
        return {"updated": await run_in_threadpool(apply)}
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
