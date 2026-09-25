"""路由共用的小工具。"""

from __future__ import annotations

from typing import List, Optional

from fastapi import Request

from ..auth import lower_query


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
