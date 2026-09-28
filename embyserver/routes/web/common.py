"""管理 API 共用的小工具。"""

from __future__ import annotations

import json

from fastapi import HTTPException, Request


async def json_body(request: Request) -> dict:
    """請求內容的 JSON 物件；沒有內容時是 {}。不是 JSON、或不是物件（例如 [1]）回 400。

    也可以當依賴用（body: dict = Depends(json_body)），讓端點寫成一般 def、在執行緒池裡跑。
    """
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="格式錯誤")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="格式錯誤：要是 JSON 物件")
    return body
