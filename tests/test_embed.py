"""在別的程式裡跑 Mi302（embed.py）和 MoviePilot 插件 Mi302（moviepilot-plugin/plugins.v3/mi302）。"""

import importlib
import json
import logging
import socket
import re
import sys
import types
from pathlib import Path

import httpx
import pytest

from embyserver import logs
from embyserver.config import Embedded
from embyserver.embed import EmbeddedServer

from fakes import wait

PLUGINS = Path(__file__).parent.parent / "moviepilot-plugin"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def up(port: int):
    """伺服器回應了就回傳 /System/Info/Public。"""
    try:
        return httpx.get(f"http://127.0.0.1:{port}/System/Info/Public", timeout=2).json()
    except httpx.HTTPError:
        return None


def port_free(port: int) -> bool:
    try:
        socket.create_server(("0.0.0.0", port)).close()
        return True
    except OSError:
        return False


class Collect(logging.Handler):
    def __init__(self, level=logging.NOTSET):
        super().__init__(level)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_embedded_server_starts_restarts_and_stops_cleanly(tmp_path: Path):
    # 宿主（MoviePilot）自己的存取日誌、root logger：Mi302 不能動它們
    host_access, extra = Collect(), Collect(logging.INFO)
    access = logging.getLogger("uvicorn.access")
    access.addHandler(host_access)
    root_before = list(logging.getLogger().handlers)
    port = free_port()
    cfg = tmp_path / "plugin" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("server:\n  port: 1\n  log_level: debug\n", encoding="utf-8")
    emb = Embedded(version="1.2.3", moviepilot_url="http://127.0.0.1:3001", moviepilot_token="mp-token")
    server = EmbeddedServer(cfg, port=port, embedded=emb, log_handlers=[extra])
    try:
        server.start()
        wait(lambda: up(port))
        # 相對的 data_dir 從設定檔所在的資料夾算起；日誌只寫在 Mi302 自己的 logger
        assert (cfg.parent / "data" / "library.db").exists()
        assert (cfg.parent / "data" / "logs" / "mi302.log").exists()
        assert logging.getLogger().handlers == root_before
        assert access.handlers[-1] is host_access and not host_access.records  # 播放器的請求不進 MoviePilot 的存取日誌
        assert any("開始服務" in r.getMessage() for r in extra.records)
        assert not [r for r in extra.records if r.levelno < logging.INFO]

        st = server.app.state
        status = st.updater.status()
        assert (status["managed"], status["version"], status["can_update"], status["can_restart"]) == ("MoviePilot", "1.2.3", False, True)
        assert "插件" in status["update_reason"] and st.updater.check()["check"]["at"] == 0  # 不向 GitHub 查
        # MoviePilot 網址、令牌沒填：用插件所在的 MoviePilot；網址填了別台，令牌就要自己填
        mp = st.moviepilot
        assert (mp.url, mp.api_token, mp.enabled) == ("http://127.0.0.1:3001", "mp-token", True)
        mp.cfg.url = "http://other:3001"
        assert (mp.url, mp.api_token, mp.enabled) == ("http://other:3001", "", False)
        mp.cfg.url = ""
        # 網頁的設定：說明插件模式，令牌本身不給
        base = f"http://127.0.0.1:{port}"
        token = httpx.post(f"{base}/web/api/setup", json={"name": "admin", "password": "pw"}).json()["token"]
        view = httpx.get(f"{base}/web/api/settings", headers={"X-Emby-Token": token}).json()
        assert view["embedded"] == {"host": "MoviePilot", "version": "1.2.3", "moviepilot_url": "http://127.0.0.1:3001",
                                    "moviepilot_token": True}
        assert view["port"] == port and "mp-token" not in json.dumps(view)

        # 網頁上的「重新啟動」：停下再開，同一個埠
        boot = st.updater.boot
        st.updater.restart()
        wait(lambda: server.app is not None and server.app.state.updater.boot != boot and up(port))
        assert server.app.state.updater.status()["managed"] == "MoviePilot"

        assert server.stop() is True
        assert not server.running and server.app is None and port_free(port)
        assert not logging.getLogger(logs.PACKAGE).handlers  # 加的 handler 都拿掉了，日誌照常往上傳
        assert logging.getLogger(logs.PACKAGE).propagate and logging.getLogger().handlers == root_before

        server.start()  # 停下後可以再開
        wait(lambda: up(port))
        assert server.stop() is True

        # 埠被占用：start 直接丟出來，不留下日誌 handler
        with socket.create_server(("0.0.0.0", port)):
            with pytest.raises(OSError):
                server.start()
        assert not server.running and not logging.getLogger(logs.PACKAGE).handlers
    finally:
        server.stop()
        access.removeHandler(host_access)


# ---------------- MoviePilot 插件 ----------------


class FakePluginBase:
    """MoviePilot 的 _PluginBase 用到的部分。"""

    data_path: Path

    def __init__(self):
        self.saved = []

    def get_data_path(self) -> Path:
        return self.data_path

    def update_config(self, config):
        self.saved.append(config)


@pytest.fixture
def plugin_module(tmp_path: Path, monkeypatch):
    """照 MoviePilot 的方式載入 app.plugins.mi302（embyserver 在插件資料夾裡，是另一份模組）。"""
    mp_log = Collect()
    fake_logger = types.SimpleNamespace(**{k: (lambda msg, _k=k: mp_log.records.append((_k, msg))) for k in ("info", "warning", "error", "debug")})
    settings = types.SimpleNamespace(PORT=3001, API_TOKEN="mp-token")
    modules = {
        "app": types.ModuleType("app"),
        "app.sdk": types.ModuleType("app.sdk"),
        "app.sdk.plugin": types.ModuleType("app.sdk.plugin"),
        "app.sdk.logging": types.ModuleType("app.sdk.logging"),
        "app.sdk.config": types.ModuleType("app.sdk.config"),
        "app.plugins": types.ModuleType("app.plugins"),
    }
    modules["app.sdk.plugin"]._PluginBase = FakePluginBase
    modules["app.sdk.logging"].logger = fake_logger
    modules["app.sdk.config"].settings = settings
    modules["app.plugins"].__path__ = [str(PLUGINS / "plugins.v3")]
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)
    FakePluginBase.data_path = tmp_path / "Mi302"
    FakePluginBase.data_path.mkdir()
    module = importlib.import_module("app.plugins.mi302")
    module.mp_log = mp_log
    try:
        yield module
    finally:
        for name in [n for n in sys.modules if n.startswith("app.plugins.mi302")]:
            del sys.modules[name]


def test_moviepilot_plugin_lifecycle(plugin_module, tmp_path: Path):
    plugin = plugin_module.Mi302()
    port = free_port()
    conf = {"enabled": True, "port": str(port), "config_path": ""}
    try:
        plugin.init_plugin(conf)
        wait(lambda: up(port))
        server = plugin._server
        assert plugin.get_state() is True
        assert server.config_path == FakePluginBase.data_path / "config.yaml"  # 沒填設定檔：放在插件資料夾
        assert (FakePluginBase.data_path / "data" / "library.db").exists()
        # 模組是插件裡那一份；Mi302 的日誌也轉到 MoviePilot 的插件日誌
        assert type(server).__module__ == "app.plugins.mi302.embyserver.embed"
        assert any(kind == "info" and "開始服務" in msg for kind, msg in plugin_module.mp_log.records)
        assert server.app.state.moviepilot.api_token == "mp-token"

        # 存設定沒改到埠號、設定檔：不重新啟動（MoviePilot 存設定時直接再呼叫 init_plugin）
        boot = server.app.state.updater.boot
        plugin.init_plugin({**conf, "port": port})
        assert plugin._server is server and server.app.state.updater.boot == boot

        # 忘記密碼：填了就重設，之後從 MoviePilot 的設定裡清掉
        plugin.init_plugin({**conf, "reset_user": "cat", "reset_password": "new-pw"})
        assert plugin.saved[-1]["reset_user"] == "" and plugin.saved[-1]["reset_password"] == ""
        r = httpx.post(f"http://127.0.0.1:{port}/Users/AuthenticateByName", json={"Username": "cat", "Pw": "new-pw"})
        assert r.status_code == 200 and server.app.state.updater.boot == boot

        page = json.dumps(plugin.get_page(), ensure_ascii=False)
        assert "Mi302 執行中" in page and f":{port}/web" in page
        form, model = plugin.get_form()
        assert model["port"] == 8096 and str(FakePluginBase.data_path / "config.yaml") in json.dumps(form, ensure_ascii=False)

        # 改埠號：舊的停下、新的開在新埠
        port2 = free_port()
        plugin.init_plugin({**conf, "port": port2})
        wait(lambda: up(port2))
        assert port_free(port)

        # 埠被占用：不丟例外（MoviePilot 的存設定會失敗），詳情頁說清楚
        with socket.create_server(("0.0.0.0", port)):
            plugin.init_plugin({**conf, "port": port})
            assert not plugin._server.running and "開不了" in json.dumps(plugin.get_page(), ensure_ascii=False)

        plugin.init_plugin({**conf, "enabled": False})
        assert plugin.get_state() is False and port_free(port2)
        assert plugin.stop_service() is True
    finally:
        plugin.stop_service()


def test_plugin_package_index_matches_plugin():
    """市場索引、插件類別、相依清單的版本一致；labels 要逗號分隔的字串（陣列會被 MoviePilot 接成一個標籤）。"""
    index = json.loads((PLUGINS / "package.v3.json").read_text(encoding="utf-8"))
    source = (PLUGINS / "plugins.v3" / "mi302" / "__init__.py").read_text(encoding="utf-8")
    project = (PLUGINS / "plugins.v3" / "mi302" / "pyproject.toml").read_text(encoding="utf-8")  # 3.10 沒有 tomllib
    assert f'plugin_version = "{index["Mi302"]["version"]}"' in source
    assert re.search(r'^version = "(.+)"$', project, re.M).group(1) == index["Mi302"]["version"]
    assert all(f'"{name}>=' in project for name in ("p115cipher", "pypinyin", "zhconv"))
    for pid, entry in index.items():
        assert isinstance(entry["labels"], str), pid
        assert (PLUGINS / "plugins.v3" / pid.lower() / "__init__.py").exists()
