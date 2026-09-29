"""FastAPI 應用程式組裝。"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from starlette.requests import ClientDisconnect

from .auth import AuthService
from .backup import Backup
from .config import Config
from .db import Database
from .dupes import DupeFinder
from .intro import IntroLearner
from .moviepilot import MoviePilot, library_series
from .organize115 import Organizer
from .reorganize import Reorganizer
from .p115 import P115Service
from .people import PeopleStore, PersonNames
from .prober import MediaProber
from .redirect import Redirector
from .routes import items, p115, playback, system, web
from . import logs, settings
from .scanner import Scanner
from .strm_sync import FULL, StrmSync
from .updater import Updater

log = logging.getLogger(__name__)
access_log = logging.getLogger("embyserver.access")

# Emby 客戶端可能加上這些前綴，路由一律以去掉前綴後的小寫路徑比對
PATH_PREFIXES = ("/emby", "/mediabrowser")
ASCII_UPPER_RE = re.compile(r"[A-Z]+")


def _ascii_lower(path: str) -> str:
    """只把英文字母轉小寫。路徑裡的人名（/Persons/Émilie）也在這裡，Python 的 lower() 會連 É 都改掉，
    但 SQLite 的 lower() 只認英文，兩邊就對不上了。"""
    return ASCII_UPPER_RE.sub(lambda m: m.group().lower(), path)


def _quiet(path: str) -> bool:
    """管理網頁自己的請求（含定時查狀態）不記，只記播放器的請求。"""
    return path == "/web" or path.startswith("/web/") or (path.startswith("/p115/") and path.endswith("/status"))


def _after_sync(app: FastAPI, result) -> None:
    """115 同步做完之後：探測新檔的媒體資訊、重新掃描變動的地方、送 MoviePilot 刮削、全量後補全缺集。"""
    config, st = app.state.config, app.state
    # 新 strm 的媒體資訊在背景探測，和掃描、刮削同時進行（互不相干）
    # 115 上換掉的檔案（pickcode 變了）舊媒體資訊已作廢，和新檔一起重新探測
    mi = config.mediainfo
    todo = result.new_files + result.replaced
    if todo and mi.enabled and mi.after_sync and st.prober.available():
        st.prober.run_in_background(todo, "sync")
    # 先只掃有變動的地方，新片馬上出現；再把新產生的 strm 交給 MoviePilot 刮削，刮好的會再掃一次
    if config.p115.strm.scan_after_sync and result.changed:
        st.scanner.scan_paths(result.changed)
    mp = st.moviepilot
    if result.new_files and mp.enabled and config.moviepilot.scrape_after_sync:
        mp.scrape(result.new_files, "sync")
    if result.mode == FULL and config.moviepilot.fill_after_full_sync and mp.can_subscribe:
        # 刮削完才有 tmdbid；每一季先向 TMDB 查已播出的集，缺集的季才建訂閱，齊全的不建
        shows = [s for s in library_series(st.db)[0] if s["tmdbid"]]
        if shows:
            mp.fill_in_background(shows, "sync")


async def _normalize_path(request: Request, call_next):
    """路徑去掉 /emby 這類前綴、英文轉小寫、去掉結尾的 /；詳細模式時記下播放器的每個請求。"""
    path = request.scope["path"]
    lower = _ascii_lower(path)
    for prefix in PATH_PREFIXES:
        if lower == prefix or lower.startswith(prefix + "/"):
            lower = lower[len(prefix):] or "/"
            break
    if len(lower) > 1:
        lower = lower.rstrip("/")
    request.scope["path"] = lower
    started = time.monotonic()
    response = await call_next(request)
    if access_log.isEnabledFor(logging.DEBUG) and not _quiet(lower):
        query = request.scope.get("query_string", b"").decode("latin-1")
        access_log.debug(
            "%s %s → %s（%d ms）%s%s",
            request.method,
            logs.redact(path + ("?" + query if query else "")),
            response.status_code,
            (time.monotonic() - started) * 1000,
            request.headers.get("user-agent", "")[:80],
            "　未實作或找不到" if response.status_code == 404 else "",
        )
    return response


def create_app(config: Config, db_path: Optional[str] = None, scan_on_start: bool = True) -> FastAPI:
    logs.attach()
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
            app.state.backup.start()
            app.state.person_names.start()
            app.state.updater.start()
        yield
        app.state.strm_sync.stop()
        app.state.prober.stop()
        app.state.backup.stop()
        app.state.person_names.stop()
        app.state.updater.stop()

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
    app.state.moviepilot = MoviePilot(config.moviepilot, config, on_done=scanner.scan_paths, db=db)
    app.state.prober = MediaProber(config.mediainfo, config, app.state.p115, db)
    app.state.backup = Backup(db, config)
    app.state.updater = Updater(config)  # 網頁上的檢查更新、更新、重新啟動
    app.state.people = PeopleStore(db, config)
    app.state.intro = IntroLearner(db, config)
    app.state.person_names = PersonNames(db, config, app.state.moviepilot)

    app.state.strm_sync = StrmSync(
        app.state.p115,
        config.p115.strm,
        on_done=lambda result: _after_sync(app, result),
        port=config.server.port,
    )
    app.state.dupes = DupeFinder(db, app.state.p115, app.state.strm_sync, scanner)  # 115 上的重複檔案
    app.state.reorganizer = Reorganizer(db, app.state.strm_sync, app.state.moviepilot)  # 集號不對的劇交給 MoviePilot 整理
    # 整理 115 網盤：命名不照 MoviePilot 格式的資料夾整個交給它整理
    app.state.organizer = Organizer(db, app.state.strm_sync, app.state.moviepilot, app.state.reorganizer)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    app.middleware("http")(_normalize_path)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        # Emby 的錯誤回應是純文字
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    @app.exception_handler(ClientDisconnect)
    async def client_gone(request: Request, exc: ClientDisconnect):
        # 瀏覽器或播放器在請求送完之前就斷了（重新整理、關掉分頁、網路斷掉）：不是伺服器的錯，這個請求也沒做任何事
        log.info("連線在請求送完之前就斷了，這個請求沒有執行：%s %s", request.method, request.url.path)
        return Response(status_code=499)  # 499 = 客戶端先關閉連線（Nginx 的慣例）

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        # 沒預料到的錯誤也回傳原因，網頁上才看得出問題在哪，完整堆疊寫進日誌
        log.exception("處理 %s %s 時發生錯誤", request.method, request.url.path)
        # 錯誤訊息可能帶著網址（含 MoviePilot 的 token），遮掉再回給客戶端
        return PlainTextResponse(f"伺服器錯誤：{type(exc).__name__}: {logs.redact(str(exc))}", status_code=500)

    app.include_router(system.router)
    app.include_router(items.router)
    app.include_router(playback.router)
    app.include_router(p115.router)
    app.include_router(web.router)
    return app
