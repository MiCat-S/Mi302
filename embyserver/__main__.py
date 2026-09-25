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
    parser.add_argument("--sync-115", action="store_true", help="從 115 同步 strm、掃描媒體庫後結束")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(args.config)
    oneshot = args.scan or args.sync_115
    app = create_app(config, scan_on_start=not oneshot)
    if args.sync_115:
        app.state.strm_sync.run()
        return
    if args.scan:
        app.state.scanner.scan_all()
        return
    uvicorn.run(app, host=config.server.host, port=config.server.port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
