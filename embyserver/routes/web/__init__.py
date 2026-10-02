"""網頁管理介面 /web 和它用的管理 API，照分頁分成幾個檔案，這裡組成一個 router 給 app 用。

詳細說明見 docs/modules.md 的「embyserver/routes/web/__init__.py」。
"""

from fastapi import APIRouter

from ..common import SafeJSONResponse
from . import aliyun, intro, moviepilot, organize, p115, scan, server, setup

router = APIRouter()
for _module in (setup, scan, p115, aliyun, organize, moviepilot, intro, server):
    # 115 的 id 超過 JavaScript 數字的精確範圍：一律用 SafeJSONResponse 改成字串
    router.include_router(_module.router, default_response_class=SafeJSONResponse)
