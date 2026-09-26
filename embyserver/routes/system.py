"""System / Users / Sessions 等雜項端點。"""

from __future__ import annotations

import json
import platform
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from ..auth import AuthContext, client_info, require_admin, require_user
from ..dto import user_dto
from .common import q, state

router = APIRouter()

EMBY_VERSION = "4.8.11.0"


def _local_address(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def _public_info(request: Request) -> dict:
    st = state(request)
    return {
        "LocalAddress": _local_address(request),
        "LocalAddresses": [_local_address(request)],
        "RemoteAddresses": [],
        "ServerName": st.config.server.name,
        "Version": EMBY_VERSION,
        "ProductName": "Emby Server",
        "OperatingSystem": "Linux",
        "Id": st.server_id,
        "StartupWizardCompleted": True,
    }


@router.api_route("/system/info/public", methods=["GET", "HEAD"])
def system_info_public(request: Request):
    return _public_info(request)


@router.api_route("/system/info", methods=["GET", "HEAD"])
def system_info(request: Request, ctx: AuthContext = Depends(require_user)):
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    info = _public_info(request)
    info.update(
        {
            "OperatingSystemDisplayName": platform.platform(),
            "HasPendingRestart": False,
            "IsShuttingDown": False,
            "SupportsLibraryMonitor": False,
            "WebSocketPortNumber": port,
            "HttpServerPortNumber": port,
            "HttpsPortNumber": port,
            "SupportsHttps": request.url.scheme == "https",
            "CanSelfRestart": False,
            "CanSelfUpdate": False,
            "CanLaunchWebBrowser": False,
            "HasUpdateAvailable": False,
            "SupportsAutoRunAtStartup": False,
            "HardwareAccelerationRequiresPremiere": False,
            "SystemUpdateLevel": "Release",
            "CompletedInstallations": [],
            "WanAddress": None,
        }
    )
    return info


@router.api_route("/system/ping", methods=["GET", "POST", "HEAD"])
def ping():
    return Response(content="Emby Server", media_type="text/plain")


@router.get("/system/endpoint")
def system_endpoint():
    return {"IsLocal": True, "IsInNetwork": True}


@router.get("/branding/configuration")
def branding():
    return {"LoginDisclaimer": "", "CustomCss": "", "SplashscreenEnabled": False}


# ---------------- Users ----------------


@router.get("/users/public")
def users_public(request: Request):
    st = state(request)
    if not st.config.server.public_users:
        return []
    return [user_dto(u, st.server_id, st.config.server.allow_download) for u in st.auth.list_users()]


@router.post("/users/authenticatebyname")
async def authenticate_by_name(request: Request):
    st = state(request)
    body: dict = {}
    ctype = request.headers.get("content-type", "")
    raw = await request.body()
    if raw:
        if "json" in ctype or raw.lstrip().startswith(b"{"):
            try:
                body = json.loads(raw)
            except ValueError:
                body = {}
        else:
            # 表單（application/x-www-form-urlencoded）自己解析：request.form() 要另裝 python-multipart
            body = dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))
    lb = {k.lower(): v for k, v in body.items()}
    username = lb.get("username") or q(request, "username") or ""
    password = lb.get("pw")
    if password is None:
        password = lb.get("password")
    if password is None:
        password = q(request, "pw", "") or ""
    info = client_info(request)

    def login():
        # 驗證密碼要算幾十毫秒的雜湊，不能擋住事件迴圈，其他人的播放請求會跟著卡
        found = st.auth.authenticate(username, password)
        if not found:
            return None, None
        return st.auth.get_user(found["id"]), st.auth.issue_token(found, info)

    user, token = await run_in_threadpool(login)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password entered.")
    return {
        "User": user_dto(user, st.server_id, st.config.server.allow_download),
        "SessionInfo": _session_info(request, user, info),
        "AccessToken": token,
        "ServerId": st.server_id,
    }


def _session_info(request: Request, user: dict, info: dict) -> dict:
    st = state(request)
    return {
        "PlayState": {"CanSeek": False, "IsPaused": False, "IsMuted": False, "RepeatMode": "RepeatNone"},
        "AdditionalUsers": [],
        "RemoteEndPoint": request.client.host if request.client else "",
        "PlayableMediaTypes": ["Audio", "Video"],
        "Id": info.get("deviceid", "") or "session",
        "ServerId": st.server_id,
        "UserId": user["id"],
        "UserName": user["name"],
        "Client": info.get("client", ""),
        "DeviceName": info.get("device", ""),
        "DeviceId": info.get("deviceid", ""),
        "ApplicationVersion": info.get("version", ""),
        "SupportedCommands": [],
        "SupportsRemoteControl": False,
    }


@router.get("/users")
def users_list(request: Request, ctx: AuthContext = Depends(require_admin)):
    st = state(request)
    return [user_dto(u, st.server_id, st.config.server.allow_download) for u in st.auth.list_users()]


@router.get("/users/{user_id}")
def user_get(user_id: str, request: Request, ctx: AuthContext = Depends(require_user)):
    st = state(request)
    if user_id.lower() != ctx.user_id.lower() and not ctx.user["is_admin"]:
        raise HTTPException(status_code=403, detail="Forbidden")
    user = st.auth.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user_dto(user, st.server_id, st.config.server.allow_download)


@router.post("/sessions/logout")
def logout(request: Request, ctx: AuthContext = Depends(require_user)):
    state(request).auth.revoke(ctx.token)
    return Response(status_code=204)


# ---------------- Sessions / 偏好設定 ----------------


@router.api_route("/sessions/capabilities", methods=["POST"])
@router.api_route("/sessions/capabilities/full", methods=["POST"])
def capabilities(ctx: AuthContext = Depends(require_user)):
    return Response(status_code=204)


@router.get("/sessions")
def sessions(ctx: AuthContext = Depends(require_user)):
    return []


@router.get("/displaypreferences/{pref_id}")
def display_prefs_get(pref_id: str, request: Request):
    return {
        "Id": pref_id,
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "RememberIndexing": False,
        "PrimaryImageHeight": 250,
        "PrimaryImageWidth": 250,
        "CustomPrefs": {},
        "ScrollDirection": "Horizontal",
        "ShowBackdrop": True,
        "RememberSorting": False,
        "ShowSidebar": False,
        "Client": q(request, "client", "emby"),
    }


@router.post("/displaypreferences/{pref_id}")
def display_prefs_set(pref_id: str):
    return Response(status_code=204)


@router.get("/users/{user_id}/groupingoptions")
def grouping_options(user_id: str):
    return []


@router.get("/plugins")
def plugins():
    return []


@router.get("/localization/{kind}")
def localization(kind: str):
    return []


@router.websocket("/embywebsocket")
async def emby_websocket(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_text()
            try:
                data = json.loads(msg)
            except ValueError:
                continue
            if data.get("MessageType") == "KeepAlive":
                await ws.send_text(json.dumps({"MessageType": "KeepAlive"}))
    except WebSocketDisconnect:
        pass


@router.post("/library/refresh")
def library_refresh(request: Request, ctx: AuthContext = Depends(require_admin)):
    import threading

    threading.Thread(target=state(request).scanner.scan_all, daemon=True).start()
    return Response(status_code=204)
