"""路由共用的小工具。"""

from __future__ import annotations

from typing import List, Optional

from fastapi import HTTPException, Request

from ..auth import AuthContext, lower_query
from ..dto import user_data_dto


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
