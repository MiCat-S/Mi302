"""網頁本身、首次設定、設定、使用者、選資料夾、API 金鑰、日誌、備份、中文化。"""

from __future__ import annotations

import os
import string
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ... import logs, settings
from ...auth import AuthContext, client_info, require_admin
from ...settings import SettingsError
from ..common import q, state
from .common import json_body

router = APIRouter()

PAGE = Path(__file__).resolve().parent.parent.parent / "web" / "admin.html"
_setup_lock = threading.Lock()


def _user_view(u: dict) -> dict:
    return {"id": u["id"], "name": u["name"], "admin": bool(u["is_admin"]), "last_login": u.get("last_login")}


@router.get("/web")
def web_page():
    # 網頁上更新 Mi302 後，瀏覽器要拿新的頁面，不能用快取的舊版
    return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


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
    body = await json_body(request)
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
    body = await json_body(request)
    st = state(request)
    before = settings.export_settings(st.config)["libraries"]

    def apply():
        settings.save(st.db, st.config, body)  # 寫設定檔
        return settings.after_change(st, before)

    try:
        notes = await run_in_threadpool(apply)
    except SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {**_settings_view(request), "notes": notes}  # notes：存完之後在背景做的事，網頁上提示


# ---------------- 使用者 ----------------


@router.get("/web/api/users")
def list_users(request: Request, ctx: AuthContext = Depends(require_admin)):
    return [_user_view(u) for u in state(request).auth.list_users()]


@router.post("/web/api/users")
async def add_user(request: Request, ctx: AuthContext = Depends(require_admin)):
    body = await json_body(request)
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
    body = await json_body(request)
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


# ---------------- API 金鑰 ----------------


@router.get("/web/api/apikeys")
def list_api_keys(request: Request, ctx: AuthContext = Depends(require_admin)):
    return state(request).auth.list_api_keys()


@router.post("/web/api/apikeys")
def create_api_key(request: Request, ctx: AuthContext = Depends(require_admin), body: dict = Depends(json_body)):
    return state(request).auth.create_api_key(str(body.get("name") or ""))


@router.delete("/web/api/apikeys/{key}")
def delete_api_key(key: str, request: Request, ctx: AuthContext = Depends(require_admin)):
    state(request).auth.delete_api_key(key)
    return Response(status_code=204)


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


# ---------------- 備份 ----------------


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


# ---------------- 中文化 ----------------


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
