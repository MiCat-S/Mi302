"""命令列入口：python -m embyserver [-c config.yaml]"""

import argparse
import logging
import os
import sys
import threading

import uvicorn

from . import logs
from .app import create_app
from .config import load_config
from .updater import restart_argv, restart_env


def main() -> None:
    parser = argparse.ArgumentParser(description="Emby 相容伺服器")
    parser.add_argument("-c", "--config", default=None, help="設定檔路徑（預設 config.yaml）")
    parser.add_argument("--scan", action="store_true", help="只掃描媒體庫後結束")
    parser.add_argument(
        "--sync-115", nargs="?", const="full", choices=("full", "incremental"), default=None,
        help="從 115 同步 strm（預設全量，incremental = 增量）、刮削與掃描後結束",
    )
    parser.add_argument(
        "--reset-password", nargs=2, metavar=("帳號", "新密碼"),
        help="忘記密碼時重設（帳號不存在會建立成管理員）後結束",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format=logs.FORMAT)
    config = load_config(args.config)
    logs.setup(config.data_path, config.server.log_level)
    oneshot = args.scan or args.sync_115 or args.reset_password
    app = create_app(config, scan_on_start=not oneshot)
    if args.reset_password:
        app.state.auth.reset_password(*args.reset_password)
        print(f"已重設 {args.reset_password[0]} 的密碼")
        return
    if args.sync_115:
        app.state.strm_sync.run(args.sync_115)
        return
    if args.scan:
        app.state.scanner.scan_all()
        return
    # 不讓 uvicorn 另外設定日誌，它的訊息才會進日誌檔和網頁；請求紀錄由 app 自己記（詳細模式）。
    # 停下時進行中的請求（例如播放器在串流本機影片）最多等 5 秒
    server = uvicorn.Server(uvicorn.Config(
        app, host=config.server.host, port=config.server.port, proxy_headers=True, forwarded_allow_ips="*",
        log_config=None, access_log=False, timeout_graceful_shutdown=5,
    ))
    restart = threading.Event()

    def request_restart() -> None:
        """網頁上的「重新啟動」「更新」：請 uvicorn 停下，停好之後 exec 自己。"""
        restart.set()
        server.should_exit = True

    app.state.updater.restart_cb = request_restart
    server.run()
    if restart.is_set():
        # 同一個程序編號換成新程式：systemd、launchd、背景執行（pid 檔）都不用另外處理
        logging.getLogger(__name__).info("重新啟動 Mi302")
        logging.shutdown()
        os.execve(sys.executable, restart_argv(), restart_env())


if __name__ == "__main__":
    main()
