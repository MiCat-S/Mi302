"""命令列入口：python -m embyserver [-c config.yaml]"""

import argparse
import logging

import uvicorn

from .app import create_app
from .config import load_config


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

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(args.config)
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
    uvicorn.run(app, host=config.server.host, port=config.server.port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
