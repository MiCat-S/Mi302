"""路由共用的小工具。"""

from __future__ import annotations

from typing import List, Optional, Tuple

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


SHELF_LIMIT = 500  # 最新、繼續觀看、接著看、人物搜尋這類首頁橫列一次最多幾項


def paging(request: Request, default_limit: Optional[int] = None, max_limit: Optional[int] = None) -> Tuple[int, Optional[int]]:
    """StartIndex、Limit。負數和看不懂的當成沒給：StartIndex 從 0、Limit 用 default_limit（None = 不限）；
    max_limit 有給時 Limit 不超過它。Limit=0 照 Emby 的意思只要總數、不要項目。
    （SQLite 的 LIMIT -1 是不限、Python 切片的負數是從後面算，兩種都不能直接用。）"""
    start = max(q_int(request, "StartIndex", 0) or 0, 0)
    limit = q_int(request, "Limit")
    if limit is None or limit < 0:
        limit = default_limit
    if limit is not None and max_limit is not None:
        limit = min(limit, max_limit)
    return start, limit


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


def as_user(request: Request, ctx: AuthContext, user_id: Optional[str]) -> AuthContext:
    """路徑 /Users/{id}/… 或 UserId 參數指定的使用者：讀寫觀看紀錄、續播點、收藏都用這個人的。
    只能指定自己；管理員（和 API 金鑰）可以代別人查、代別人標記。沒指定就是登入的人。
    id 比對不分大小寫、不管有沒有連字號（有的播放器把 id 寫成 GUID 格式）。"""
    want = (user_id or "").replace("-", "").lower()
    if not want or want == ctx.user_id.replace("-", "").lower():
        return ctx
    if not ctx.user["is_admin"]:
        raise HTTPException(status_code=403, detail="Forbidden")
    user = state(request).auth.get_user(want)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return AuthContext(user, ctx.token, via_api_key=ctx.via_api_key)


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
