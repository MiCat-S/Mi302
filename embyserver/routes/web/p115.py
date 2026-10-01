"""115：瀏覽、回收站、選同步目錄、雲下載（離線下載）；115 上的重複檔案、空資料夾；媒體資訊（從 115 探測）。"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ...auth import AuthContext, require_admin
from ...browse115 import list_folder
from ...emptydirs import EmptyDirsError
from ...offline115 import OfflineError
from ...p115 import P115Error
from ...p115_open import P115OpenError
from ...reorganize import ReorgError
from ...probe_select import ProbeFilter, missing_paths, missing_titles, title_names
from ..common import q, q_int, state
from .common import json_body

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------- 雲下載（離線下載） ----------------


def _offline(fn, *args):
    try:
        return fn(*args)
    except OfflineError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/web/api/115/offline")
async def offline_tasks(request: Request, ctx: AuthContext = Depends(require_admin)):
    """115 雲下載的任務（page；一頁 30 個）和這個月還能加幾個（quota）。"""
    st = state(request)
    return await run_in_threadpool(_offline, st.offline.tasks, q_int(request, "page") or 1)


@router.post("/web/api/115/offline")
async def offline_add(request: Request, ctx: AuthContext = Depends(require_admin)):
    """加雲下載任務：{urls: 一行一個（磁力、ed2k、http、https、ftp），folder: 存到的 115 資料夾路徑，空的用 115 預設}。
    回傳每個連結的結果；看不懂的連結在 rejected。"""
    st = state(request)
    body = await json_body(request)
    return await run_in_threadpool(_offline, st.offline.add, str(body.get("urls") or ""), str(body.get("folder") or ""))


@router.post("/web/api/115/offline/delete")
async def offline_delete(request: Request, ctx: AuthContext = Depends(require_admin)):
    """刪雲下載任務：{hashes: [info_hash], files: 連同 115 上下載好的檔案一起刪}。"""
    st = state(request)
    body = await json_body(request)
    hashes = body.get("hashes") if isinstance(body.get("hashes"), list) else []
    return await run_in_threadpool(_offline, st.offline.delete, [str(h) for h in hashes], bool(body.get("files")))


@router.post("/web/api/115/offline/retry")
async def offline_retry(request: Request, ctx: AuthContext = Depends(require_admin)):
    """重新加入失敗的任務：{hash, url, folder_id}。先刪掉那一筆失敗的紀錄（不動檔案），再用原來的連結加到原來的資料夾。"""
    st = state(request)
    body = await json_body(request)
    try:
        folder_id = int(body.get("folder_id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="folder_id 要是數字")
    return await run_in_threadpool(_offline, st.offline.retry, str(body.get("hash") or ""), str(body.get("url") or ""),
                                   folder_id)


@router.post("/web/api/115/offline/clear")
async def offline_clear(request: Request, ctx: AuthContext = Depends(require_admin)):
    """清掉已完成（{what: "done"}）或已失敗（"failed"）的任務紀錄，不動檔案。"""
    st = state(request)
    body = await json_body(request)
    return await run_in_threadpool(_offline, st.offline.clear, str(body.get("what") or ""))


# ---------------- 瀏覽 115、回收站 ----------------


@router.get("/web/api/115/files")
def browse_115_files(request: Request, ctx: AuthContext = Depends(require_admin)):
    """瀏覽 115：資料夾的子資料夾和檔案，影片附上在 Mi302 媒體庫裡的樣子。給 cid（和 path）或只給 path。
    檔案一次最多 1000 個，offset 要下一批。"""
    st = state(request)
    path = (q(request, "path") or "").strip()
    cid = q_int(request, "cid")
    try:
        if cid is None:
            path = "/" + path.strip("/") if path.strip("/") else "/"
            cid = st.p115.dir_id(path) if path != "/" else 0
            if not cid and path != "/":
                raise HTTPException(status_code=400, detail=f"115 上沒有這個資料夾：{path}")
        return list_folder(st.p115, st.db, st.strm_sync.tasks, cid, path, q_int(request, "offset", 0) or 0)
    except (P115Error, P115OpenError) as exc:
        raise HTTPException(status_code=400, detail=f"讀不到 115：{exc}")


@router.post("/web/api/115/delete")
async def browse_delete(request: Request, ctx: AuthContext = Depends(require_admin)):
    """瀏覽 115 裡勾的刪掉：{parent: 現在看的資料夾 id, ids: [資料夾或檔案 id]}。移到 115 回收站（可以還原），
    資料夾連同裡面所有檔案；同步目錄裡的本機 strm、nfo 和媒體庫跟著拿掉。要用掃碼登入 115。"""
    st = state(request)
    body = await json_body(request)
    ids = [int(i) for i in body.get("ids") or [] if str(i).isdigit()]
    try:
        parent = int(body.get("parent") or 0)
        result = await run_in_threadpool(st.reorganizer.delete_in_folder, parent, ids)
    except (ReorgError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    for i in ids:  # 釘在「整理 115 網盤」的也拿掉
        st.organizer.unpin(f"d{i}")
    return result


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
    q 比對檔名和路徑（不同版本也比對片名），offset、limit 分頁。
    kind=big：大檔案一個一個列，大的在前；min_size（位元組，至少 1 GB）、type（movie／episode／none＝不在媒體庫）。"""
    offset = max(q_int(request, "offset", 0) or 0, 0)
    limit = min(max(q_int(request, "limit", 20) or 20, 1), 100)
    if q(request, "kind") == "big":
        return state(request).dupes.big(q_int(request, "min_size", 0) or 0, q(request, "type") or "", q(request, "q") or "",
                                         offset, limit)
    kind = "versions" if q(request, "kind") == "versions" else "exact"
    return state(request).dupes.groups(q(request, "q") or "", offset, limit, kind)


@router.post("/web/api/dupes/delete")
async def dupes_delete(request: Request, ctx: AuthContext = Depends(require_admin)):
    """刪重複：{"overrides": {"檔案 id": true/false}} 逐個指定要不要刪，沒指定的照預設。

    use_suggestions：沒指定的檔案要不要照建議刪（不是建議保留的都刪）。完全相同（kind=exact，預設）預設 true，
    不同版本（kind=versions）預設 false。{"sha1", "size"}（完全相同）或 {"grp"}（不同版本）只處理那一組。
    送進 115 回收站，每組至少留一份；本機 strm 跟著刪、觀看紀錄轉到保留的那份。在背景跑。
    {"dry_run": true} 只算會刪幾個、多大，不刪（網頁上的數量和確認框用；不用登入 115）。
    kind=big（大檔案）：overrides 裡勾了的；use_suggestions 為真時加上符合 {min_size, type, q} 的全部。不必留一份。
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
    if body.get("kind") == "big":
        try:
            min_size = int(body.get("min_size") or 0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="min_size 要是整數")
        plan = await run_in_threadpool(st.dupes.big_plan, overrides, bool(body.get("use_suggestions")), min_size,
                                       str(body.get("type") or ""), str(body.get("q") or ""))
    else:
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


@router.post("/web/api/dupes/stop")
def dupes_stop(request: Request, ctx: AuthContext = Depends(require_admin)):
    """停止找重複（上次的結果不換）或刪除（做完手上這一批就停）。"""
    st = state(request)
    return {"stopped": st.dupes.cancel(), "job": st.dupes.job.as_dict()}


@router.get("/web/api/dupes/log")
def dupes_log(request: Request, ctx: AuthContext = Depends(require_admin)):
    """最近刪掉的重複檔案、大檔案（到 115 回收站找回用）。"""
    return state(request).dupes.recent_deletions(min(max(q_int(request, "limit", 50) or 50, 1), 500))


# ---------------- 115 上的空資料夾 ----------------


@router.get("/web/api/empty-dirs")
def empty_dirs_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    """掃描或刪除的進度、上次掃描的範圍和結果（幾個空資料夾、裡面的檔案共多大、沒列的有幾個）。"""
    return state(request).empty_dirs.summary()


@router.post("/web/api/empty-dirs/scan")
async def empty_dirs_scan(request: Request, ctx: AuthContext = Depends(require_admin)):
    """開始找空資料夾：{"paths": ["/影視"]}，不給就用同步任務的 115 目錄。在背景跑。"""
    body = await json_body(request)
    paths = body.get("paths")
    if paths is not None and (not isinstance(paths, list) or not all(isinstance(p, str) for p in paths)):
        raise HTTPException(status_code=400, detail="paths 要是 115 路徑的清單")
    st = state(request)
    if not st.p115.logged_in:
        raise HTTPException(status_code=400, detail="尚未登入 115")
    try:
        return {"started": st.empty_dirs.scan_in_background(paths)}
    except EmptyDirsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/web/api/empty-dirs/list")
def empty_dirs_list(request: Request, ctx: AuthContext = Depends(require_admin)):
    """空資料夾照路徑排：q 比對路徑，offset、limit 分頁。"""
    offset = max(q_int(request, "offset", 0) or 0, 0)
    limit = min(max(q_int(request, "limit", 50) or 50, 1), 200)
    return state(request).empty_dirs.items(q(request, "q") or "", offset, limit)


@router.post("/web/api/empty-dirs/delete")
async def empty_dirs_delete(request: Request, ctx: AuthContext = Depends(require_admin)):
    """刪空資料夾：{"overrides": {"資料夾 id": true/false}} 逐個勾的；{"all": true} 再加上符合搜尋 q 的全部
    （overrides 取消勾的除外）。刪之前再到 115 上確認一次，送進 115 回收站，本機對應的資料夾跟著拿掉。在背景跑。
    {"dry_run": true} 只算會刪幾個、裡面的檔案多大，不刪（網頁上的數量和確認框用）。"""
    body = await json_body(request)
    raw = body.get("overrides") or {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="overrides 格式錯誤")
    try:
        overrides = {int(k): bool(v) for k, v in raw.items()}
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="overrides 格式錯誤")
    st = state(request)
    plan = await run_in_threadpool(st.empty_dirs.plan, overrides, bool(body.get("all")), str(body.get("q") or ""))
    result = {"started": False, "count": len(plan), "size": sum(r["size"] for r in plan)}
    if body.get("dry_run"):
        return result
    try:
        st.empty_dirs.delete_in_background(plan)
    except EmptyDirsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {**result, "started": True}


@router.post("/web/api/empty-dirs/stop")
def empty_dirs_stop(request: Request, ctx: AuthContext = Depends(require_admin)):
    """停止掃描（上次的結果不換）或刪除（做完手上這一個就停）。"""
    st = state(request)
    return {"stopped": st.empty_dirs.cancel(), "job": st.empty_dirs.job.as_dict()}
