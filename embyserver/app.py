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
from .redirect import Redirector
from .routes import items, playback, system
from .scanner import Scanner

log = logging.getLogger(__name__)

# Emby 客戶端可能加上這些前綴，路由一律以去掉前綴後的小寫路徑比對
PATH_PREFIXES = ("/emby", "/mediabrowser")


def create_app(config: Config, db_path: Optional[str] = None, scan_on_start: bool = True) -> FastAPI:
    db = Database(db_path or config.data_path / "library.db")
    server_id = db.get_meta("server_id")
    if not server_id:
        server_id = uuid.uuid4().hex
        db.set_meta("server_id", server_id)

    auth = AuthService(db, config.api_keys)
    for user in config.users:
        auth.ensure_user(user.name, user.password, user.admin)
    scanner = Scanner(db, config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if scan_on_start:
            threading.Thread(target=scanner.scan_all, daemon=True).start()
        yield

    app = FastAPI(title="Emby 相容伺服器", lifespan=lifespan, docs_url="/api-docs", redoc_url=None)
    app.state.config = config
    app.state.db = db
    app.state.server_id = server_id
    app.state.auth = auth
    app.state.scanner = scanner
    app.state.redirector = Redirector(config.redirect)

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
    return app
