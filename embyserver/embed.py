"""在別的程式裡跑 Mi302（MoviePilot 插件）：自己的執行緒跑 uvicorn，可以停下、再開，不動整個程序共用的設定。

詳細說明見 docs/modules.md 的「embyserver/embed.py」。
"""

from __future__ import annotations

import logging
import socket
import threading
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import uvicorn
from uvicorn.protocols.http.auto import AutoHTTPProtocol

from . import logs
from .app import close_app, create_app, services, stop_workers
from .config import Config, Embedded, load_config
from .strm_sync import SERVER_URL_META_KEY

log = logging.getLogger(__name__)

STOP_WAIT = 60.0  # 停下時最多等幾秒：背景工作最多 30 秒（workers.SHUTDOWN_WAIT）、進行中的請求 5 秒，再留一點
FORCE_WAIT = 10.0  # 等不到時強制結束（不等請求、不跑 lifespan），自己停背景工作、關資料庫，再等這麼久
LEFTOVER_WAIT = 30.0  # 上一次等不到的背景工作，開新的之前再等它們這麼久


class QuietProtocol(AutoHTTPProtocol):  # type: ignore[misc, valid-type]
    """不讓 uvicorn 記每個請求：Mi302 自己記（詳細模式）。

    uvicorn 看的是整個程序共用的 uvicorn.access 有沒有 handler（MoviePilot 自己的有），不看這台伺服器的設定；
    也不能用 Config(access_log=False)，它會把整個程序 uvicorn.access 的 handler 清掉，MoviePilot 的存取日誌就沒了。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.access_log = False


def bind(host: str, port: int) -> socket.socket:
    """在呼叫的執行緒綁定埠：被占用時馬上丟 OSError。交給 uvicorn 的話，它在背景執行緒裡 sys.exit，誰也看不到。"""
    if host in ("::", "") and socket.has_dualstack_ipv6():
        return socket.create_server(("", port), family=socket.AF_INET6, dualstack_ipv6=True, backlog=2048)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return socket.create_server((host, port), family=family, backlog=2048)


class EmbeddedServer:
    """一台跑在背景執行緒的 Mi302。start 之後 stop，可以再 start；網頁上的「重新啟動」也是 stop 再 start。

    config_path：設定檔（沒有的話第一次在網頁上儲存時建立）；相對的 data_dir 從它所在的資料夾算起。
    port：蓋過設定檔的 server.port（插件設定頁填的）；None 照設定檔。
    log_handlers：Mi302 的日誌另外也交給這些 handler（例如轉送到 MoviePilot 的插件日誌）。"""

    def __init__(self, config_path, port: Optional[int] = None, embedded: Optional[Embedded] = None,
                 log_handlers: Sequence[logging.Handler] = ()):
        self.config_path = Path(config_path).expanduser().resolve()
        self.port = port
        self.embedded = embedded or Embedded()
        self.log_handlers = list(log_handlers)
        self.app = None  # 起來之後是 FastAPI app；網頁、插件頁從這裡看狀態
        self.config: Optional[Config] = None
        self.error = ""  # 上次在背景啟動失敗的原因
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._lock = threading.RLock()  # start、stop、重新啟動不同時做
        self._leftover: List = []  # 上次停下時等不到背景工作的服務
        self._ready: List[Callable] = []  # 起來之後要做的事（when_ready）

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        """讀設定、綁定埠，在背景執行緒開始服務（不等 Mi302 準備好）。設定檔有錯、埠被占用時直接丟出來。"""
        with self._lock:
            if self.running:
                raise RuntimeError("Mi302 已經在執行")
            self._wait_leftover()
            config = load_config(str(self.config_path))
            config.base_dir = str(self.config_path.parent)
            config.embedded = self.embedded
            if self.port:
                config.server.port = int(self.port)
            logs.setup(config.data_path, config.server.log_level, embedded=True, extra=self.log_handlers)
            try:
                sock = bind(config.server.host, config.server.port)
            except OSError:
                logs.detach()
                raise
            self.config, self.error = config, ""
            self._stopping.clear()
            self._thread = threading.Thread(target=self._run, args=(config, sock), name="Mi302", daemon=True)
            self._thread.start()
            log.info("Mi302 在 %s:%s 開始服務，管理網頁 /web；資料夾 %s", config.server.host, config.server.port, config.data_path)

    def _run(self, config: Config, sock: socket.socket) -> None:
        app = None
        try:
            app = create_app(config)
            app.state.updater.restart_cb = self.restart_later
            self.app = app
            for fn in self._take_ready():
                self._call(fn, app)
            server = uvicorn.Server(uvicorn.Config(
                app, host=config.server.host, port=config.server.port, proxy_headers=True, forwarded_allow_ips="*",
                log_config=None, http=QuietProtocol, timeout_graceful_shutdown=5, lifespan="on",
            ))
            self._server = server
            if self._stopping.is_set():  # 還在準備時就叫停了（stop 那邊看到 server 的話會設 should_exit，兩邊總有一邊接住）
                return
            server.run(sockets=[sock])
            if not server.started and not self._stopping.is_set():
                self.error = "Mi302 沒有啟動起來，原因見日誌"
        except BaseException as exc:  # uvicorn 啟動失敗是 SystemExit
            self.error = f"Mi302 啟動失敗：{type(exc).__name__}: {exc}"
            log.exception("Mi302 啟動失敗")
        finally:
            sock.close()
            if app is not None and not getattr(app.state, "closed", False):
                # lifespan 沒跑到結束（沒起來、或被強制停下）：自己停背景工作、關資料庫
                try:
                    app.state.unstopped = stop_workers(app)
                finally:
                    close_app(app)

    def stop(self, timeout: float = STOP_WAIT) -> bool:
        """停下並等它結束。停乾淨了（埠放開、背景工作都結束）回傳 True；等不到回傳 False，下次 start 會先等它們。"""
        with self._lock:
            thread = self._thread
            if thread is None:
                return not self._leftover
            self._stopping.set()
            if self._server is not None:
                self._server.should_exit = True
            thread.join(timeout)
            if thread.is_alive() and self._server is not None:
                log.warning("Mi302 %s 秒內沒有停下，不等進行中的請求，強制結束", int(timeout))
                self._server.force_exit = True
                thread.join(FORCE_WAIT)
            if thread.is_alive():
                log.error("Mi302 停不下來，之後重新啟動前會再等它")
                return False
            app, self.app, self._thread, self._server = self.app, None, None, None
            left = list(getattr(app.state, "unstopped", [])) if app is not None else []
            if left:
                self._leftover = services(app)
            log.info("Mi302 已停止%s", f"（{'、'.join(left)} 還沒停下）" if left else "")
            logs.detach()
            return not left

    def restart_later(self) -> None:
        """網頁上的「重新啟動」（updater 一秒後在另一個執行緒呼叫）：停下再開，重新讀設定檔。"""
        with self._lock:
            if not self.stop():
                self.error = "Mi302 沒有完全停下，先不重新啟動；請稍後在 MoviePilot 的插件頁再儲存一次"
                log.error(self.error)
                return
            try:
                self.start()
            except Exception as exc:
                self.error = f"重新啟動失敗：{exc}"
                log.exception("重新啟動 Mi302 失敗")

    def when_ready(self, fn: Callable) -> None:
        """Mi302 準備好之後呼叫 fn(app)；已經好了就馬上呼叫。"""
        app = self.app
        if app is not None:
            self._call(fn, app)
            return
        with self._lock:
            self._ready.append(fn)

    def _take_ready(self) -> List[Callable]:
        with self._lock:
            ready, self._ready = self._ready, []
        return ready

    @staticmethod
    def _call(fn: Callable, app) -> None:
        try:
            fn(app)
        except Exception:
            log.exception("Mi302 啟動後的工作出錯")

    def _wait_leftover(self) -> None:
        """上次停下時等不到的背景工作，再等一下；還不停就不開新的，免得兩邊同時寫同一批 strm。"""
        if not self._leftover:
            return
        left: List[str] = []
        for s in self._leftover:
            left += s.workers.join(LEFTOVER_WAIT / len(self._leftover))
        if left:
            raise RuntimeError(f"上一次的 Mi302 還有工作沒停下（{'、'.join(left)}），請稍後再試")
        self._leftover = []

    def web_url(self) -> str:
        """管理網頁的網址：strm 網址有填用它，否則用管理員上次開網頁的網址；都沒有是空的（插件頁改用 MoviePilot 的主機名稱）。"""
        app = self.app
        if app is None:
            return ""
        try:
            base = app.state.config.p115.strm.base_url or app.state.db.get_meta(SERVER_URL_META_KEY) or ""
        except Exception:  # 資料庫正在關
            return ""
        return base.rstrip("/") + "/web" if base else ""
