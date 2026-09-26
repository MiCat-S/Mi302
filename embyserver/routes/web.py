"""網頁管理介面 /web 與它用的管理 API：首次設定、媒體庫、使用者、進階設定、選資料夾。"""

from __future__ import annotations

import json
import os
import string
import threading
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .. import logs, settings
from ..auth import AuthContext, client_info, require_admin
from ..dto import image_tag
from ..moviepilot import library_series
from ..p115 import P115Error
from ..p115_open import P115OpenError
from ..settings import SettingsError
from .common import q, q_int, state

router = APIRouter()

PAGE = Path(__file__).resolve().parent.parent / "web" / "admin.html"
_setup_lock = threading.Lock()


async def _body(request: Request):
    try:
        return json.loads(await request.body() or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="格式錯誤")


def _user_view(u: dict) -> dict:
    return {"id": u["id"], "name": u["name"], "admin": bool(u["is_admin"]), "last_login": u.get("last_login")}


@router.get("/web")
def web_page():
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


@router.get("/web/115")
def web_115_page():
    return RedirectResponse(url="/web#115", status_code=302)


# ---------------- 首次設定 ----------------


@router.get("/web/api/setup")
def setup_status(request: Request):
    return {"needed": not state(request).auth.list_users()}


@router.post("/web/api/setup")
async def setup(request: Request):
    """還沒有任何帳號時，建立第一個管理員；之後這個端點就失效。"""
    body = await _body(request)
    name, password = str(body.get("name") or "").strip(), str(body.get("password") or "")
    if not name or not password:
        raise HTTPException(status_code=400, detail="帳號和密碼都要填")
    st = state(request)

    def create():
        with _setup_lock:
            if st.auth.list_users():
                raise HTTPException(status_code=403, detail="已經設定過管理員")
            user = st.auth.create_user(name, password, True)  # 算密碼雜湊要幾十毫秒
        return st.auth.issue_token(user, client_info(request))

    return {"token": await run_in_threadpool(create)}


# ---------------- 設定 ----------------


def _settings_view(request: Request, file_error: str = "") -> dict:
    st = state(request)
    return {
        **settings.export_settings(st.config),
        "port": st.config.server.port,
        "config_path": str(Path(st.config.path).resolve()) if st.config.path else "",
        "file_error": file_error,
    }


def _refresh(request: Request) -> str:
    """設定檔被手動改過時重新套用；檔案有錯時回傳錯誤訊息，繼續用目前的設定。"""
    try:
        settings.refresh(state(request))
    except SettingsError as exc:
        return str(exc)
    return ""


@router.get("/web/api/settings")
def get_settings(request: Request, ctx: AuthContext = Depends(require_admin)):
    return _settings_view(request, _refresh(request))


@router.put("/web/api/settings")
async def put_settings(request: Request, ctx: AuthContext = Depends(require_admin)):
    body = await _body(request)
    st = state(request)
    before = settings.export_settings(st.config)["libraries"]

    def apply():
        settings.save(st.db, st.config, body)  # 寫設定檔
        settings.after_change(st, before)

    try:
        await run_in_threadpool(apply)
    except SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _settings_view(request)


# ---------------- 媒體庫掃描 ----------------


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
    body = await _body(request)
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


# ---------------- 使用者 ----------------


@router.get("/web/api/users")
def list_users(request: Request, ctx: AuthContext = Depends(require_admin)):
    return [_user_view(u) for u in state(request).auth.list_users()]


@router.post("/web/api/users")
async def add_user(request: Request, ctx: AuthContext = Depends(require_admin)):
    body = await _body(request)
    if not str(body.get("password") or ""):
        raise HTTPException(status_code=400, detail="密碼不可空白")
    try:
        user = await run_in_threadpool(  # 算密碼雜湊要幾十毫秒，不佔事件迴圈
            state(request).auth.create_user,
            str(body.get("name") or ""), str(body.get("password") or ""), bool(body.get("admin")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _user_view(user)


@router.put("/web/api/users/{user_id}")
async def edit_user(user_id: str, request: Request, ctx: AuthContext = Depends(require_admin)):
    body = await _body(request)
    if "password" in body and not str(body["password"] or ""):
        raise HTTPException(status_code=400, detail="密碼不可空白")
    try:
        user = await run_in_threadpool(
            state(request).auth.update_user,
            user_id,
            password=str(body["password"]) if "password" in body else None,
            admin=bool(body["admin"]) if "admin" in body else None,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="找不到使用者")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _user_view(user)


@router.delete("/web/api/users/{user_id}")
def delete_user(user_id: str, request: Request, ctx: AuthContext = Depends(require_admin)):
    try:
        state(request).auth.delete_user(user_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="找不到使用者")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(status_code=204)


# ---------------- 選資料夾 ----------------


def _roots() -> list:
    if os.name == "nt":
        return [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
    return ["/"]


@router.get("/web/api/browse")
def browse_local(request: Request, ctx: AuthContext = Depends(require_admin)):
    """列出伺服器上某個資料夾的子資料夾，讓網頁用點選的方式挑媒體庫路徑。"""
    raw = q(request, "path") or ""
    if not raw:
        # 有 /media 就從它開始（媒體資料夾常放這裡），沒有就從根目錄
        raw = "/media" if Path("/media").is_dir() else _roots()[0]
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"資料夾不存在：{raw}")
    try:
        dirs = sorted(
            (e.name for e in os.scandir(path) if e.is_dir(follow_symlinks=True) and not e.name.startswith(".")),
            key=str.lower,
        )
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"無法讀取：{exc}")
    parent = str(path.parent) if path.parent != path else None
    return {"path": str(path), "parent": parent, "dirs": dirs, "roots": _roots()}


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


# ---------------- MoviePilot ----------------


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


@router.get("/web/api/intro/status")
def intro_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    """片頭片尾：學到幾季、最近學到的。"""
    return state(request).intro.status()


@router.post("/web/api/intro/clear")
async def intro_clear(request: Request, ctx: AuthContext = Depends(require_admin)):
    """清掉學到的片頭片尾（{"season_id": id} 只清一季，沒有就全清）。"""
    body = await _body(request)
    sid = body.get("season_id")
    return {"removed": state(request).intro.clear(int(sid) if str(sid or "").isdigit() else None)}


@router.get("/web/api/people/status")
def people_status(request: Request, ctx: AuthContext = Depends(require_admin)):
    """演職人員中文名：有幾位、查到幾位、來源、還有幾位沒查。"""
    st = state(request)
    return {
        "chinese_people": st.config.server.chinese_people, "chinese_genres": st.config.server.chinese_genres,
        **st.person_names.status(),
    }


@router.post("/web/api/people/resolve")
def people_resolve(request: Request, ctx: AuthContext = Depends(require_admin)):
    """現在就去查還沒查的中文名（在背景跑）。"""
    st = state(request)
    if not st.config.server.chinese_people:
        raise HTTPException(status_code=400, detail="請先開啟「演職人員顯示中文名」並儲存")
    return {"started": st.person_names.run_in_background()}


@router.get("/web/api/backups")
def list_backups(request: Request, ctx: AuthContext = Depends(require_admin)):
    bk = state(request).backup
    return {"keep": bk.keep, "dir": str(bk.dir), "last": int(bk.last()) or None, "items": bk.items()}


@router.post("/web/api/backups")
async def backup_now(request: Request, ctx: AuthContext = Depends(require_admin)):
    try:
        name = await run_in_threadpool(state(request).backup.run)
    except Exception as exc:  # 磁碟滿、權限、sqlite 錯誤都直接告訴使用者
        raise HTTPException(status_code=500, detail=f"備份失敗：{type(exc).__name__}: {exc}")
    return {"name": name}


@router.get("/web/api/backups/{name}")
def download_backup(name: str, request: Request, ctx: AuthContext = Depends(require_admin)):
    path = state(request).backup.path_of(name)
    if not path:
        raise HTTPException(status_code=404, detail="找不到這個備份")
    return FileResponse(path, media_type="application/octet-stream", filename=name)


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


@router.post("/web/api/mediainfo/probe")
def mediainfo_probe(request: Request, ctx: AuthContext = Depends(require_admin)):
    """探測媒體庫裡所有還沒有媒體資訊的影片（在背景跑）。"""
    st = state(request)
    if not st.config.mediainfo.enabled:
        raise HTTPException(status_code=400, detail="請先開啟「整庫探測」並儲存")
    if not st.prober.available():
        raise HTTPException(status_code=400, detail="找不到 ffprobe，請先安裝 ffmpeg")
    started = st.prober.run_in_background(None, "manual")
    return {"started": started, "result": st.prober.result.as_dict()}


@router.get("/web/api/series")
def list_series(request: Request, ctx: AuthContext = Depends(require_admin)):
    """媒體庫裡的劇和每一季的集數、集號空洞；q 搜尋劇名，gaps=1 只列有空洞的，offset、limit 分頁。"""
    offset = max(q_int(request, "offset", 0) or 0, 0)
    limit = min(max(q_int(request, "limit", 20) or 20, 1), 200)
    items, total = library_series(
        state(request).db, q(request, "q") or "", q(request, "gaps") in ("1", "true"), limit=limit, offset=offset
    )
    return {"items": items, "total": total, "offset": offset, "more": offset + len(items) < total}


@router.post("/web/api/moviepilot/fill")
async def moviepilot_fill(request: Request, ctx: AuthContext = Depends(require_admin)):
    """補全缺集：{"series": [id, ...]} 只送這些劇；空的就送所有有 tmdbid 的劇。"""
    st = state(request)
    mp = st.moviepilot
    body = await _body(request)
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


# ---------------- 日誌 ----------------


@router.get("/web/api/logs")
def get_logs(request: Request, ctx: AuthContext = Depends(require_admin)):
    """最近的日誌；after = 上次拿到的最後序號，只回傳比它新的。"""
    try:
        after = int(q(request, "after") or 0)
        limit = min(max(int(q(request, "limit") or 500), 1), 3000)
    except ValueError:
        raise HTTPException(status_code=400, detail="after、limit 要是數字")
    result = logs.MEMORY.query(after, q(request, "level") or "INFO", q(request, "q") or "", limit)
    path = logs.file_path()
    result.update(
        log_level=state(request).config.server.log_level,
        file=str(path) if path else None,
        files=logs.files(),
    )
    return result


@router.get("/web/api/logs/download")
def download_logs(request: Request, ctx: AuthContext = Depends(require_admin)):
    """下載日誌檔（name 可指定換下來的舊檔）；沒有日誌檔時下載記憶體裡的紀錄。"""
    name = q(request, "name") or logs.LOG_FILE
    path = logs.file_path()
    if path and name in {f["name"] for f in logs.files()}:
        return FileResponse(path.with_name(name), media_type="text/plain; charset=utf-8", filename=name)
    return PlainTextResponse(
        logs.MEMORY.dump(), headers={"Content-Disposition": 'attachment; filename="mi302.log"'}
    )


# ---------------- API 金鑰 ----------------


@router.get("/web/api/apikeys")
def list_api_keys(request: Request, ctx: AuthContext = Depends(require_admin)):
    return state(request).auth.list_api_keys()


@router.post("/web/api/apikeys")
async def create_api_key(request: Request, ctx: AuthContext = Depends(require_admin)):
    body = await _body(request)
    return state(request).auth.create_api_key(str(body.get("name") or ""))


@router.delete("/web/api/apikeys/{key}")
def delete_api_key(key: str, request: Request, ctx: AuthContext = Depends(require_admin)):
    state(request).auth.delete_api_key(key)
    return Response(status_code=204)
