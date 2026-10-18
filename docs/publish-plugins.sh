#!/bin/bash
# 把 moviepilot-plugin/ 發布到外掛倉庫 MiCat-S/Mi302-MoviePilot-Plugins：倉庫整個換成這個資料夾的內容，所以改外掛請改 Mi302 倉庫裡的檔案。
# MoviePilot 的插件市場只讀倉庫根目錄的 package.v3.json（main 分支），所以外掛要另外一個倉庫才能在市場裡出現。
set -euo pipefail
cd "$(dirname "$0")/.."
REMOTE="${1:-https://github.com/MiCat-S/Mi302-MoviePilot-Plugins.git}"
REV="$(git rev-parse --short HEAD)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if ! err="$(git clone -q "$REMOTE" "$TMP/plugins" 2>&1)"; then
	echo "無法取得外掛倉庫：${err}" >&2
	exit 1
fi
cd "$TMP/plugins"
git checkout -q -B main
find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cd - >/dev/null
cp -R moviepilot-plugin/. "$TMP/plugins/"
find "$TMP/plugins" -name __pycache__ -type d -prune -exec rm -rf {} +
cd "$TMP/plugins"
git add -A
if git diff --cached --quiet; then
	echo "外掛倉庫已經是最新的。"
	exit 0
fi
git -c user.name="$(git -C "$OLDPWD" config user.name)" -c user.email="$(git -C "$OLDPWD" config user.email)" \
	commit -q -m "同步自 Mi302 ${REV}"
git push -q origin main
echo "已發布到 ${REMOTE%.git}"
