"""網頁管理介面 /web 和它用的管理 API，照分頁分成幾個檔案，這裡組成一個 router 給 app 用。

- setup：網頁本身、首次設定、設定、使用者、選資料夾、API 金鑰、日誌、備份、中文化
- scan：媒體庫掃描、批量新增媒體庫的建議
- intro：片頭片尾
- moviepilot：刮削、補全缺集
- organize：整理 115 網盤（整理、集號不對的劇、刪除）
- p115：瀏覽 115、回收站、重複檔案、媒體資訊
- server：版本、檢查更新、更新、重新啟動
"""

from fastapi import APIRouter

from ..common import SafeJSONResponse
from . import intro, moviepilot, organize, p115, scan, server, setup

router = APIRouter()
for _module in (setup, scan, p115, organize, moviepilot, intro, server):
    # 115 的 id 超過 JavaScript 數字的精確範圍：一律用 SafeJSONResponse 改成字串
    router.include_router(_module.router, default_response_class=SafeJSONResponse)
