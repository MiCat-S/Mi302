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
from ..p115 import P115Error
from ..p115_open import P115OpenError
from ..settings import SettingsError
from .common import q, state

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
    return {"scanning": st.scanner.scanning, "current": st.scanner.current, "libraries": libs}


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
        # 預設從 /media 開始（Docker 預設掛載點），沒有就從根目錄
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
    return {"enabled": mp.enabled, "result": mp.result.as_dict()}


@router.post("/web/api/moviepilot/scrape")
def moviepilot_scrape(request: Request, ctx: AuthContext = Depends(require_admin)):
    """把媒體庫裡還沒有 nfo 的影片都送去 MoviePilot 刮削。"""
    mp = state(request).moviepilot
    if not mp.enabled:
        raise HTTPException(status_code=400, detail="請先填好 MoviePilot 網址與 API 令牌並儲存")
    started = mp.scrape_in_background(None, "manual")
    return {"started": started, "result": mp.result.as_dict()}


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
