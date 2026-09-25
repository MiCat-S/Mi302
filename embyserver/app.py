"""FastAPI 應用程式組裝。"""

from __future__ import annotations

import logging
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from .auth import AuthService
from .config import Config
from .db import Database
from .p115 import P115Service
from .redirect import Redirector
from .routes import items, p115, playback, system, web
from . import settings
from .scanner import Scanner
from .strm_sync import StrmSync

log = logging.getLogger(__name__)

# Emby 客戶端可能加上這些前綴，路由一律以去掉前綴後的小寫路徑比對
PATH_PREFIXES = ("/emby", "/mediabrowser")


def create_app(config: Config, db_path: Optional[str] = None, scan_on_start: bool = True) -> FastAPI:
    db = Database(db_path or config.data_path / "library.db")
    server_id = db.get_meta("server_id")
    if not server_id:
        server_id = uuid.uuid4().hex
        db.set_meta("server_id", server_id)

    # 網頁上存過的設定蓋過設定檔
    settings.load_saved(db, config)
    auth = AuthService(db, config.api_keys)
    for user in config.users:
        auth.ensure_user(user.name, user.password, user.admin)
    scanner = Scanner(db, config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if scan_on_start:
            threading.Thread(target=scanner.scan_all, daemon=True).start()
            app.state.strm_sync.start_schedule()
        yield

    app = FastAPI(title="Emby 相容伺服器", lifespan=lifespan, docs_url="/api-docs", redoc_url=None)
    app.state.config = config
    app.state.db = db
    app.state.server_id = server_id
    app.state.auth = auth
    app.state.scanner = scanner
    app.state.p115 = P115Service(
        db, config.p115.cookies, config.p115.app, config.p115.timeout, open_app_id=config.p115.open_app_id
    )
    app.state.redirector = Redirector(config.redirect, app.state.p115)
    app.state.strm_sync = StrmSync(
        app.state.p115,
        config.p115.strm,
        on_done=scanner.scan_all,
        port=config.server.port,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    @app.middleware("http")
    async def normalize_path(request: Request, call_next):
        path = request.scope["path"]
        lower = path.lower()
        for prefix in PATH_PREFIXES:
            if lower == prefix or lower.startswith(prefix + "/"):
                lower = lower[len(prefix):] or "/"
                break
        if len(lower) > 1:
            lower = lower.rstrip("/")
        request.scope["path"] = lower
        response = await call_next(request)
        if response.status_code == 404:
            log.debug("未實作或找不到：%s %s", request.method, path)
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        # Emby 的錯誤回應是純文字
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    app.include_router(system.router)
    app.include_router(items.router)
    app.include_router(playback.router)
    app.include_router(p115.router)
    app.include_router(web.router)
    return app
