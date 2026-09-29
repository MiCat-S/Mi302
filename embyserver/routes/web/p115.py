"""115：瀏覽、整理 115 網盤、回收站、選同步目錄；115 上的重複檔案；媒體資訊（從 115 探測）。"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ...auth import AuthContext, require_admin
from ...browse115 import list_folder
from ...organize115 import OrganizeError
from ...p115 import P115Error
from ...p115_open import P115OpenError
from ...probe_select import ProbeFilter, missing_paths, missing_titles, title_names
from ..common import q, q_int, state
from .common import json_body

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------- 瀏覽 115、回收站 ----------------


@router.get("/web/api/115/files")
def browse_115_files(request: Request, ctx: AuthContext = Depends(require_admin)):
    """瀏覽 115：資料夾的子資料夾和檔案，影片附上在 Mi302 媒體庫裡的樣子。給 cid（和 path）或只給 path。"""
    st = state(request)
    path = (q(request, "path") or "").strip()
    cid = q_int(request, "cid")
    try:
        if cid is None:
            path = "/" + path.strip("/") if path.strip("/") else "/"
            cid = st.p115.dir_id(path) if path != "/" else 0
            if not cid and path != "/":
                raise HTTPException(status_code=400, detail=f"115 上沒有這個資料夾：{path}")
        return list_folder(st.p115, st.db, st.strm_sync.tasks, cid, path)
    except (P115Error, P115OpenError) as exc:
        raise HTTPException(status_code=400, detail=f"讀不到 115：{exc}")


@router.get("/web/api/115/organize")
def organize_list(request: Request, ctx: AuthContext = Depends(require_admin)):
    """整理 115 網盤：命名不照 MoviePilot 重命名格式的資料夾（q 搜尋、kind=series|movie、offset、limit；refresh=1 重新找）。
    只看媒體庫和 115 同步紀錄，不向 115 請求；附上比對用的格式、缺什麼設定（ready）、目前的整理工作（job）。"""
    st = state(request)
    offset, limit = max(q_int(request, "offset") or 0, 0), min(max(q_int(request, "limit") or 50, 1), 200)
    result = st.organizer.list(q(request, "q") or "", q(request, "kind") or "", offset, limit, q(request, "refresh") == "1")
    return {**result, "offset": offset, "ready": st.reorganizer.ready(), "job": st.reorganizer.job.as_dict()}


@router.post("/web/api/115/organize/preview")
async def organize_preview(request: Request, ctx: AuthContext = Depends(require_admin)):
    """請 MoviePilot 只算不做：{id, tmdbid, type: tv|movie, seasons: {部分: 季}, scrape}。
    回傳每個檔案的新位置和 Mi302 的檢查；有能整理的就給預覽代碼 token，用 /web/api/moviepilot/reorganize/execute 執行。"""
    st = state(request)
    body = await json_body(request)
    if not st.reorganizer.ready()["login"]:
        raise HTTPException(status_code=400, detail="MoviePilot 的手動整理只接受帳號登入，請在「MoviePilot 帳號密碼」填好再儲存")
    seasons = body.get("seasons") if isinstance(body.get("seasons"), dict) else {}
    seasons = {str(k): int(v) if str(v).strip().isdigit() else None for k, v in seasons.items()}
    try:
        return await run_in_threadpool(st.organizer.preview, str(body.get("id") or ""), str(body.get("tmdbid") or ""),
                                       str(body.get("type") or ""), seasons, bool(body.get("scrape", True)))
    except OrganizeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/web/api/115/recyclebin")
def recyclebin_list(request: Request, ctx: AuthContext = Depends(require_admin)):
    """115 回收站的一頁：數量、檔名、大小、刪除時間、原本的資料夾。"""
    st = state(request)
    offset, limit = max(q_int(request, "offset") or 0, 0), min(max(q_int(request, "limit") or 50, 1), 200)
    try:
        page = st.p115.recycle_bin(offset, limit)
    except (P115Error, P115OpenError) as exc:
        raise HTTPException(status_code=400, detail=f"讀不到 115 回收站：{exc}")
    return {**page, "offset": offset, "limit": limit, "via": "open" if st.p115.open.authorized else "cookie"}


@router.post("/web/api/115/recyclebin/clean")
async def recyclebin_clean(request: Request, ctx: AuthContext = Depends(require_admin)):
    """清空 115 回收站（永久刪除）：{"confirm": "清空", "password": 安全密鑰}。沒帶 confirm 不做。"""
    body = await json_body(request)
    if body.get("confirm") != "清空":
        raise HTTPException(status_code=400, detail="要在 confirm 帶上「清空」才會清空回收站")
    st = state(request)
    try:
        via = await run_in_threadpool(st.p115.recycle_bin_clean, str(body.get("password") or ""))
    except P115Error as exc:
        raise HTTPException(status_code=400, detail=f"清空回收站失敗：{exc}")
    log.warning("%s 清空了 115 回收站（%s）", (ctx.user or {}).get("name") or "管理員", "開放平台" if via == "open" else "cookie")
    return {"ok": True, "via": via}


@router.get("/web/api/115/browse")
def browse_115(request: Request, ctx: AuthContext = Depends(require_admin)):
    """列出 115 上某個目錄的子目錄，讓網頁用點選的方式挑同步目錄。"""
    svc = state(request).p115
    if not svc.logged_in:
        raise HTTPException(status_code=400, detail="尚未登入 115")
    path = "/" + (q(request, "path") or "/").strip("/")
    try:
        entries = svc.list_dir(svc.dir_id(path))
    except (P115Error, P115OpenError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    dirs = sorted((e["name"] for e in entries if e["is_dir"]), key=str.lower)
    parent = None if path == "/" else ("/" + path.strip("/").rpartition("/")[0]).rstrip("/") or "/"
    return {"path": path, "parent": parent, "dirs": dirs}


# ---------------- 媒體資訊 ----------------


@router.get("/web/api/mediainfo/status")
def mediainfo_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    """媒體資訊：有幾支影片已經有、ffprobe 在不在、115 熔斷、上次探測的結果。"""
    st = state(request)
    videos = "i.type IN ('Movie','Episode')"
    return {
        "enabled": st.config.mediainfo.enabled,
        "ffprobe": st.prober.available(),
        "total": st.db.one(f"SELECT COUNT(*) AS c FROM items i WHERE {videos}")["c"],
        "have": st.db.one(f"SELECT COUNT(*) AS c FROM items i JOIN media_info m ON m.path=i.path WHERE {videos}")["c"],
        "breaker": st.p115.breaker.status(),
        "usage": st.prober.usage(),
        "on_demand": {
            "enabled": st.config.mediainfo.on_demand, "queue": st.prober.queue_size(),
            "done": st.prober.on_demand_done, "failed": st.prober.on_demand_failed,
        },
        "result": st.prober.result.as_dict(),
    }


@router.get("/web/api/mediainfo/titles")
def mediainfo_titles(request: Request, ctx: AuthContext = Depends(require_admin)):
    """還缺媒體資訊的電影和劇，照「先做哪些」排好、分頁；篩選條件見 probe_select.ProbeFilter。

    也回傳符合條件還缺的電影、集數，以及媒體庫清單（給下拉選單）。
    """
    st = state(request)
    flt = ProbeFilter.from_params(request.query_params)
    offset = max(q_int(request, "offset", 0) or 0, 0)
    limit = min(max(q_int(request, "limit", 20) or 20, 1), 200)
    out = missing_titles(st.db, flt, limit, offset)
    out["libraries"] = [dict(r) for r in st.db.query("SELECT id, name FROM items WHERE type='CollectionFolder' ORDER BY id")]
    return out


@router.post("/web/api/mediainfo/probe")
async def mediainfo_probe(request: Request, ctx: AuthContext = Depends(require_admin)):
    """在背景探測還沒有媒體資訊的影片。

    內容（都可以不給）：篩選條件（q、library、kind、year_from、year_to、order，見 ProbeFilter）、
    limit 這次最多幾支（0 = 全部符合的）、ids 只做這幾部電影或劇。回傳挑到幾支、有沒有開始。
    """
    body = await json_body(request)
    st = state(request)
    if not st.config.mediainfo.enabled:
        raise HTTPException(status_code=400, detail="請先開啟「批次探測」並儲存")
    if not st.prober.available():
        raise HTTPException(status_code=400, detail="找不到 ffprobe，請先安裝 ffmpeg")
    flt = ProbeFilter.from_params(body)
    try:
        limit = max(int(body.get("limit") or 0), 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="「這次最多幾支」要填數字")
    ids = [int(i) for i in body.get("ids") or [] if str(i).isdigit()]

    def start():
        if st.prober.busy():
            return [], False
        paths = missing_paths(st.db, flt, limit, ids)
        base = title_names(st.db, ids) if ids else flt.describe()
        spec = {"filter": flt, "ids": ids, "base": base}
        return paths, bool(paths) and st.prober.run_in_background(paths, "manual", _probe_label(base, limit), limit, spec)

    paths, started = await run_in_threadpool(start)
    return {"started": started, "count": len(paths), "busy": st.prober.busy() and not started,
            "result": st.prober.result.as_dict()}


def _probe_label(base: str, limit: int) -> str:
    return base + (f"・最多 {limit} 支" if limit else "")


@router.post("/web/api/mediainfo/probe/limit")
async def mediainfo_probe_limit(request: Request, ctx: AuthContext = Depends(require_admin)):
    """提取中改「這次最多幾支」：{"limit": N}（0 = 全部符合的），直接套用到正在跑的這一批。"""
    body = await json_body(request)
    try:
        limit = max(int(body.get("limit") or 0), 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="「這次最多幾支」要填數字")
    st = state(request)
    spec = st.prober.batch_spec

    def apply():
        if not st.prober.busy() or not st.prober.result.running:
            raise HTTPException(status_code=409, detail="現在沒有在提取")
        if not spec:
            raise HTTPException(status_code=409, detail="這一批是同步後自動開始的，不能改數量")
        # 照原本的條件和順序重新挑；多挑一些，扣掉這一批已經做過、排隊中的還夠
        more = 0 if limit == 0 else limit + st.prober.result.total
        candidates = missing_paths(st.db, spec["filter"], more, spec["ids"])
        return st.prober.retarget(candidates, limit, _probe_label(spec["base"], limit))

    total = await run_in_threadpool(apply)
    if total is None:
        raise HTTPException(status_code=409, detail="現在沒有在提取")
    return {"total": total, "result": st.prober.result.as_dict()}


@router.post("/web/api/mediainfo/stop")
def mediainfo_stop(request: Request, ctx: AuthContext = Depends(require_admin)):
    """停止正在跑的這一批：排隊的不做了，手上正在做的做完就停。"""
    st = state(request)
    return {"stopping": st.prober.cancel_batch(), "result": st.prober.result.as_dict()}


# ---------------- 115 上的重複檔案 ----------------


@router.get("/web/api/dupes")
def dupes_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    """找重複的進度和上次結果：幾組、幾個檔案、照建議刪可以省多少。"""
    return state(request).dupes.summary()


@router.post("/web/api/dupes/scan")
async def dupes_scan(request: Request, ctx: AuthContext = Depends(require_admin)):
    """開始找重複：{"paths": ["/影視"]}，不給就用同步任務的 115 目錄。在背景跑。"""
    body = await json_body(request)
    paths = body.get("paths")
    if paths is not None and (not isinstance(paths, list) or not all(isinstance(p, str) for p in paths)):
        raise HTTPException(status_code=400, detail="paths 要是 115 路徑的清單")
    st = state(request)
    if not st.p115.logged_in:
        raise HTTPException(status_code=400, detail="尚未登入 115")
    return {"started": st.dupes.scan_in_background(paths)}


@router.post("/web/api/dupes/prefer")
async def dupes_prefer(request: Request, ctx: AuthContext = Depends(require_admin)):
    """不同版本建議保留哪種解析度：{"prefer": "1080" | "2160" | "highest"}；已找到的結果當場重算。"""
    body = await json_body(request)
    st = state(request)
    try:
        await run_in_threadpool(st.dupes.set_prefer, str(body.get("prefer") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return st.dupes.summary()


@router.get("/web/api/dupes/groups")
def dupes_groups(request: Request, ctx: AuthContext = Depends(require_admin)):
    """重複的組，可以省最多空間的在前面；kind=exact（完全相同，預設）或 versions（不同版本），
    q 比對檔名和路徑（不同版本也比對片名），offset、limit 分頁。"""
    offset = max(q_int(request, "offset", 0) or 0, 0)
    limit = min(max(q_int(request, "limit", 20) or 20, 1), 100)
    kind = "versions" if q(request, "kind") == "versions" else "exact"
    return state(request).dupes.groups(q(request, "q") or "", offset, limit, kind)


@router.post("/web/api/dupes/delete")
async def dupes_delete(request: Request, ctx: AuthContext = Depends(require_admin)):
    """刪重複：{"overrides": {"檔案 id": true/false}} 逐個指定要不要刪，沒指定的照預設。

    use_suggestions：沒指定的檔案要不要照建議刪（不是建議保留的都刪）。完全相同（kind=exact，預設）預設 true，
    不同版本（kind=versions）預設 false。{"sha1", "size"}（完全相同）或 {"grp"}（不同版本）只處理那一組。
    送進 115 回收站，每組至少留一份；本機 strm 跟著刪、觀看紀錄轉到保留的那份。在背景跑。
    {"dry_run": true} 只算會刪幾個、多大，不刪（網頁上的數量和確認框用；不用登入 115）。
    """
    body = await json_body(request)
    raw = body.get("overrides") or {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="overrides 格式錯誤")
    try:
        overrides = {int(k): bool(v) for k, v in raw.items()}
        size = int(body["size"]) if body.get("sha1") else None
    except (TypeError, ValueError, KeyError):
        raise HTTPException(status_code=400, detail="格式錯誤")
    st = state(request)
    kind = "versions" if body.get("kind") == "versions" else "exact"
    grp = str(body["grp"]) if body.get("grp") else None
    use_suggestions = bool(body.get("use_suggestions", kind == "exact"))
    try:
        plan = await run_in_threadpool(st.dupes.plan, overrides, body.get("sha1"), size, kind, grp, use_suggestions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if body.get("dry_run"):
        return {"started": False, "count": len(plan), "size": sum(r["size"] for r in plan)}
    if not st.p115.cookies:
        raise HTTPException(status_code=400, detail="刪除 115 上的檔案要用掃碼登入（cookie）")
    if st.dupes.busy():
        raise HTTPException(status_code=409, detail="正在找重複或刪重複，等它做完")
    started = st.dupes.delete_in_background(plan)
    return {"started": started, "count": len(plan), "size": sum(r["size"] for r in plan)}


@router.get("/web/api/dupes/log")
def dupes_log(request: Request, ctx: AuthContext = Depends(require_admin)):
    """最近刪掉的重複檔案（到 115 回收站找回用）。"""
    return state(request).dupes.recent_deletions(min(max(q_int(request, "limit", 50) or 50, 1), 500))
