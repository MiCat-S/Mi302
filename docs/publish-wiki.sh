#!/bin/bash
# 把 docs/wiki 發布到 GitHub Wiki：wiki 整個換成 docs/wiki 的內容，所以改文件請改倉庫裡的檔案。
# GitHub 要先在網頁上建立過第一頁，wiki 的 git 倉庫才會存在；沒有的話這個腳本會提醒。
set -euo pipefail

cd "$(dirname "$0")/.."
REMOTE="${1:-https://github.com/MiCat-S/Mi302.wiki.git}"
REV="$(git rev-parse --short HEAD)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if ! git clone -q "$REMOTE" "$TMP/wiki" 2>/dev/null; then
	echo "Wiki 還沒建立：先到 https://github.com/MiCat-S/Mi302/wiki 按「Create the first page」隨便存一頁，再執行一次。" >&2
	exit 1
fi

find "$TMP/wiki" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp docs/wiki/*.md "$TMP/wiki/"

cd "$TMP/wiki"
git add -A
if git diff --cached --quiet; then
	echo "Wiki 已經是最新的。"
	exit 0
fi
git commit -q -m "同步倉庫 docs/wiki（$REV）"
git push -q origin HEAD
echo "已發布到 https://github.com/MiCat-S/Mi302/wiki"
