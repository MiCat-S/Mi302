"""管理網頁（單一檔案）的腳本語法：用 node --check 檢查，沒有 node 的環境略過。"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "embyserver" / "web" / "admin.html"


@pytest.mark.skipif(not shutil.which("node"), reason="沒有 node")
def test_admin_page_script_parses(tmp_path: Path):
    scripts = re.findall(r"<script>(.*?)</script>", PAGE.read_text(encoding="utf-8"), re.S)
    assert scripts
    js = tmp_path / "admin.js"
    js.write_text("\n".join(scripts), encoding="utf-8")
    r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
