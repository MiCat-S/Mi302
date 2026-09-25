"""網頁管理介面 /web 與它用的管理 API：首次設定、媒體庫、使用者、進階設定、選資料夾。"""

from __future__ import annotations

import json
import os
import string
import threading
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import settings
from ..auth import AuthContext, client_info, require_admin
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
    with _setup_lock:
        if st.auth.list_users():
            raise HTTPException(status_code=403, detail="已經設定過管理員")
        user = st.auth.create_user(name, password, True)
    return {"token": st.auth.issue_token(user, client_info(request))}


# ---------------- 設定 ----------------


def _settings_view(request: Request) -> dict:
    st = state(request)
    return {**settings.export_settings(st.config), "port": st.config.server.port}


@router.get("/web/api/settings")
def get_settings(request: Request, ctx: AuthContext = Depends(require_admin)):
    return _settings_view(request)


@router.put("/web/api/settings")
async def put_settings(request: Request, ctx: AuthContext = Depends(require_admin)):
    body = await _body(request)
    st = state(request)
    before = settings.export_settings(st.config)["libraries"]
    try:
        settings.save(st.db, st.config, body)
    except SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # P115Service 建立時複製了這兩個值，要同步過去
    st.p115.app = st.config.p115.app
    st.p115.open.default_app_id = st.config.p115.open_app_id
    if settings.export_settings(st.config)["libraries"] != before:
        threading.Thread(target=st.scanner.scan_all, daemon=True).start()
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
    libs = [
        {
            "name": lib.name,
            "count": counts.get(lib.name, 0),
            "missing": [p for p in lib.paths if not Path(p).expanduser().is_dir()],
        }
        for lib in st.config.libraries
    ]
    return {"scanning": st.scanner.scanning, "libraries": libs}


@router.post("/web/api/scan")
def scan_now(request: Request, ctx: AuthContext = Depends(require_admin)):
    threading.Thread(target=state(request).scanner.scan_all, daemon=True).start()
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
        user = state(request).auth.create_user(
            str(body.get("name") or ""), str(body.get("password") or ""), bool(body.get("admin"))
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
        user = state(request).auth.update_user(
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
