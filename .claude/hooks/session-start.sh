#!/bin/bash
# Claude Code 雲端工作階段啟動時安裝依賴，讓測試可以直接跑。本機不做事。
# 用 uv 照 uv.lock 裝進 .venv；不安裝專案本身（避免改到被追蹤的 embyserver.egg-info），改用 PYTHONPATH。
# 雲端容器的系統 pip 是 Debian 修改版，建 zhconv 會失敗，所以不用系統 pip。
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
	exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

if command -v uv >/dev/null 2>&1; then
	uv sync --frozen --extra test --no-install-project --quiet
else
	[ -x .venv/bin/python ] || python3 -m venv .venv
	.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt pytest
fi

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
	{
		echo "export VIRTUAL_ENV=\"$CLAUDE_PROJECT_DIR/.venv\""
		echo "export PATH=\"$CLAUDE_PROJECT_DIR/.venv/bin:\$PATH\""
		echo "export PYTHONPATH=\"$CLAUDE_PROJECT_DIR\""
	} >>"$CLAUDE_ENV_FILE"
fi
