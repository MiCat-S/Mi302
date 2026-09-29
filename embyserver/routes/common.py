"""路由共用的小工具。"""

from __future__ import annotations

from typing import List, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ..auth import AuthContext, lower_query
from ..dto import user_data_dto

MAX_SAFE_INT = 2 ** 53 - 1  # JavaScript 的 Number 能精確表示的最大整數


def js_safe(value):
    """比 JavaScript 能精確表示的還大的整數改成字串。115 的檔案、資料夾 id 有 19 位，當成數字給網頁會被四捨五入，
    送回來就對不上（刪錯、列錯資料夾）。伺服器收到 id 一律 int() 轉回來，字串、數字都接受。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > MAX_SAFE_INT else value
    if isinstance(value, dict):
        return {k: js_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [js_safe(v) for v in value]
    return value


class SafeJSONResponse(JSONResponse):
    """管理網頁的 API 用：大整數（115 的 id）改成字串再送出。"""

    def render(self, content) -> bytes:
        return super().render(js_safe(content))


def q(request: Request, name: str, default: Optional[str] = None) -> Optional[str]:
    value = lower_query(request).get(name.lower())
    return value if value not in (None, "") else default


def q_int(request: Request, name: str, default: Optional[int] = None) -> Optional[int]:
    value = q(request, name)
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def q_bool(request: Request, name: str) -> Optional[bool]:
    value = q(request, name)
    if value is None:
        return None
    return value.lower() in ("true", "1", "yes")


def q_list(request: Request, name: str) -> List[str]:
    value = q(request, name)
    if not value:
        return []
    return [v.strip() for v in value.replace("|", ",").split(",") if v.strip()]


def state(request: Request):
    return request.app.state


def set_user_data(request: Request, ctx: AuthContext, item_id: str, **fields) -> dict:
    """改這個使用者對一個項目的資料（看過、續播點、收藏、最後播放時間），回傳 Emby 的 UserData；項目不存在時 404。"""
    st = state(request)
    row = st.db.get_item(item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Item not found")
    st.db.execute(
        "INSERT OR IGNORE INTO user_data(user_id, item_id) VALUES(?, ?)", (ctx.user_id, row["id"])
    )
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        st.db.execute(
            f"UPDATE user_data SET {sets} WHERE user_id=? AND item_id=?",
            (*fields.values(), ctx.user_id, row["id"]),
        )
    return user_data_dto(st.db, ctx.user_id, row)
