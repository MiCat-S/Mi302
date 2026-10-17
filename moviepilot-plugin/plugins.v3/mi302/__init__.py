"""Mi302：把 Mi302（Emby 相容伺服器：115 同步成 strm、302 直連播放）放進 MoviePilot 裡跑，不用另外安裝。

程式就是 Mi302 本體（embyserver/，在 Mi302 倉庫裡是連到根目錄 embyserver/ 的連結，發布時換成真的檔案），
由 embyserver/embed.py 在背景執行緒開自己的埠（預設 8096）：播放器照樣連 http://<主機>:8096，
管理網頁照樣是 http://<主機>:8096/web，所有設定（115、同步、媒體庫、使用者）都在那裡。
這裡只管開關、埠號和設定檔放哪裡；MoviePilot 的網址和 API 令牌不用填，自動用插件所在的 MoviePilot。

- 設定檔預設放在插件資料夾（<MoviePilot 設定目錄>/plugins/Mi302/config.yaml），資料庫、日誌、備份在旁邊的 data/。
  原本單獨安裝的 Mi302 要搬進來：先停掉原本的服務，再把「設定檔」填成原本的 config.yaml（例如 /root/Mi302/config.yaml），
  資料原地沿用，不用複製。
- 儲存設定時只有開關、埠號、設定檔改了才重新啟動 Mi302；網頁上的「重新啟動」只重新啟動 Mi302，不動 MoviePilot。
- 更新到插件市場更新；Mi302 網頁上的「檢查更新」在插件模式不用。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

DEFAULT_PORT = 8096


class _ToMoviePilotLog(logging.Handler):
    """Mi302 的日誌（info 以上）也寫進 MoviePilot 的插件日誌：MoviePilot 看呼叫的模組認出是這個插件。"""

    def __init__(self) -> None:
        super().__init__(logging.INFO)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = f"{record.name.rpartition('.')[2]}：{record.getMessage()}"
            if record.levelno >= logging.ERROR:
                logger.error(text)
            elif record.levelno >= logging.WARNING:
                logger.warning(text)
            else:
                logger.info(text)
        except Exception:
            self.handleError(record)


def _port(value) -> int:
    try:
        port = int(str(value).strip() or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


class Mi302(_PluginBase):
    plugin_name = "Mi302"
    plugin_desc = "Emby 相容伺服器：115 資料夾同步成 strm，播放時 302 直連 115，播放器照 Emby 的方式連線；跑在 MoviePilot 裡，不用另外安裝。"
    plugin_icon = "https://raw.githubusercontent.com/MiCat-S/Mi302/main/embyserver/web/icon-192.png"
    plugin_version = "1.0.0"
    plugin_author = "MiCat-S"
    author_url = "https://github.com/MiCat-S/Mi302"
    plugin_order = 97
    auth_level = 1

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._want: Optional[Tuple[int, str]] = None  # 目前這台 Mi302 是照什麼開的（埠號、設定檔）
        self._server = None  # embyserver.embed.EmbeddedServer
        self._error = ""

    # ---------------- 外掛的基本介面 ----------------

    def init_plugin(self, config: Optional[dict] = None) -> None:
        # 存設定時 MoviePilot 直接在同一個實例上再呼叫一次，不會先 stop_service：要自己先停舊的
        config = dict(config or {})
        enabled = bool(config.get("enabled"))
        want = (_port(config.get("port")), str(config.get("config_path") or "").strip())
        reset = (str(config.get("reset_user") or "").strip(), str(config.get("reset_password") or ""))
        if enabled and want == self._want and self._server is not None and self._server.running:
            self._enabled = True
        else:
            if self.stop_service() is False:
                raise RuntimeError("上一次的 Mi302 還沒停下來，請稍後再儲存一次")
            self._enabled, self._want = enabled, want
            if enabled:
                self._start(*want)
        if reset[0] and reset[1]:
            self._reset_password(*reset)
            config.update(reset_user="", reset_password="")  # 密碼不留在 MoviePilot 的設定裡
            self.update_config(config)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        default_path = str(self.get_data_path() / "config.yaml")
        return [
            {"component": "VForm", "content": [
                {"component": "VRow", "content": [
                    _col({"component": "VSwitch", "props": {"model": "enabled", "label": "啟用"}}, md=4),
                    _col({"component": "VTextField", "props": {
                        "model": "port", "label": "埠號", "type": "number", "placeholder": str(DEFAULT_PORT),
                        "hint": "播放器和管理網頁連這個埠；Docker 要把它發布出來（或用 host 網路）", "persistent-hint": True}}, md=8),
                ]},
                {"component": "VRow", "content": [
                    _col({"component": "VTextField", "props": {
                        "model": "config_path", "label": "設定檔", "placeholder": default_path,
                        "hint": "留空 = 放在插件資料夾。原本單獨安裝的 Mi302 填它的 config.yaml，資料原地沿用（要先停掉原本的服務）",
                        "persistent-hint": True}}),
                ]},
                {"component": "VRow", "content": [
                    _col({"component": "VAlert", "props": {
                        "type": "info", "variant": "tonal",
                        "text": "Mi302 是 Emby 相容的伺服器：115 的資料夾同步成 strm，播放時 302 直連 115。啟用後用瀏覽器開 "
                                "http://<主機>:<埠號>/web 完成設定（建立管理員、115 掃碼登入、同步任務、媒體庫），"
                                "播放器新增 Emby 伺服器 http://<主機>:<埠號>。MoviePilot 的網址和 API 令牌不用填，自動用這個 MoviePilot。"}}),
                ]},
                {"component": "VRow", "content": [
                    _col({"component": "VTextField", "props": {
                        "model": "reset_user", "label": "忘記密碼：帳號", "autocomplete": "off",
                        "hint": "填帳號和新密碼後儲存就重設（帳號不存在時建立成管理員），儲存後自動清空", "persistent-hint": True}}, md=6),
                    _col({"component": "VTextField", "props": {
                        "model": "reset_password", "label": "新密碼", "type": "password", "autocomplete": "new-password"}}, md=6),
                ]},
            ]},
        ], {"enabled": False, "port": DEFAULT_PORT, "config_path": "", "reset_user": "", "reset_password": ""}

    def get_page(self) -> List[dict]:
        server = self._server
        running = bool(server and server.running)
        error = self._error or (server.error if server else "")
        blocks: List[dict] = []
        if not self._enabled:
            blocks.append(_alert("Mi302 沒有啟用。到插件設定打開「啟用」並儲存。"))
        elif running:
            app = server.app
            since = f"（{_ago(app.state.updater.started)}啟動）" if app is not None else ""
            blocks.append(_alert(f"Mi302 執行中{since}，播放器和管理網頁連 {self._want[0]} 埠。", "success"))
        if error:
            blocks.append(_alert(error, "error"))
        if running:
            url = server.web_url()
            if url:
                blocks.append({"component": "VBtn", "props": {
                    "href": url, "target": "_blank", "color": "primary", "variant": "tonal", "class": "mb-4",
                    "prepend-icon": "mdi-open-in-new"}, "text": f"打開管理網頁 {url}"})
            else:
                # 不知道外面怎麼連進來：用瀏覽器現在連 MoviePilot 的主機名稱，換成 Mi302 的埠
                port = self._want[0]
                blocks.append({"component": "div", "props": {"class": "mb-4"}, "html": (
                    f'<a class="v-btn v-btn--variant-tonal text-primary pa-2" target="_blank" rel="noopener" href="#" '
                    f'onclick="this.href=\'http://\'+location.hostname+\':{port}/web\'">打開管理網頁（http://&lt;這台主機&gt;:{port}/web）</a>')})
        if server is not None and server.config is not None:
            cfg = server.config
            blocks.append(_table(["項目", "值"], [
                ["設定檔", str(server.config_path)],
                ["資料夾（資料庫、日誌、備份）", str(cfg.data_path)],
                ["Mi302 日誌", str(cfg.data_path / "logs" / "mi302.log") + "（管理網頁的「日誌」頁也看得到）"],
            ]))
        return blocks

    def stop_service(self) -> Optional[bool]:
        server = self._server
        if server is None:
            return True
        return server.stop()

    # ---------------- 開、關 ----------------

    def _start(self, port: int, config_path: str) -> None:
        self._error = ""
        try:
            from .embyserver.config import Embedded
            from .embyserver.embed import EmbeddedServer
        except ImportError as exc:  # 相依套件（p115cipher、pypinyin、zhconv）沒裝好
            self._error = f"Mi302 少了需要的套件：{exc}。請到插件頁重新安裝 Mi302，MoviePilot 會自動安裝。"
            logger.error(self._error)
            return
        from app.sdk.config import settings

        path = Path(config_path).expanduser() if config_path else self.get_data_path() / "config.yaml"
        embedded = Embedded(version=self.plugin_version, moviepilot_url=f"http://127.0.0.1:{settings.PORT}",
                            moviepilot_token=str(settings.API_TOKEN or ""))
        if self._server is None:
            self._server = EmbeddedServer(path, port=port, embedded=embedded, log_handlers=[_ToMoviePilotLog()])
        else:  # 同一個實例改了埠號或設定檔：沿用（上次沒停下的背景工作它記得，開新的之前會先等）
            self._server.config_path, self._server.port, self._server.embedded = path.resolve(), port, embedded
        try:
            self._server.start()
        except OSError as exc:
            self._error = f"Mi302 開不了 {port} 埠：{exc.strerror or exc}。埠被別的程式占用（例如原本單獨安裝的 Mi302 還在跑）時請先停掉它，或換一個埠號。"
            logger.error(self._error)
            return
        except Exception as exc:
            self._error = f"Mi302 啟動失敗：{exc}"
            logger.error(self._error)

    def _reset_password(self, name: str, password: str) -> None:
        if self._server is None or not self._enabled:
            logger.warning("Mi302 沒有啟用，沒有重設密碼")
            return

        def reset(app) -> None:
            app.state.auth.reset_password(name, password)
            logger.info(f"Mi302 已重設 {name} 的密碼")

        self._server.when_ready(reset)


# ---------------- 設定頁、詳情頁用的小零件 ----------------


def _col(item: dict, md: int = 12) -> dict:
    return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": [item]}


def _alert(text: str, kind: str = "info") -> dict:
    return {"component": "VAlert", "props": {"type": kind, "variant": "tonal", "text": text, "class": "mb-2"}}


def _table(heads: List[str], rows: List[List[str]]) -> dict:
    return {"component": "VTable", "props": {"hover": True, "class": "mb-4"}, "content": [
        {"component": "thead", "content": [{"component": "tr", "content": [{"component": "th", "text": h} for h in heads]}]},
        {"component": "tbody", "content": [{"component": "tr", "content": [{"component": "td", "text": c} for c in row]} for row in rows]},
    ]}


def _ago(at: float) -> str:
    s = time.time() - at
    return "剛剛" if s < 60 else f"{int(s // 60)} 分鐘前" if s < 3600 else f"{int(s // 3600)} 小時前"
