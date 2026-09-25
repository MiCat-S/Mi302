"""呼叫外部服務用的 httpx Client：把連線錯誤轉成呼叫端認得的例外，附上看得懂的原因。"""

from __future__ import annotations

from typing import Type

import httpx


def describe(exc: httpx.HTTPError) -> str:
    host = ""
    try:
        host = exc.request.url.host
    except RuntimeError:  # 例外沒有綁定 request
        pass
    if isinstance(exc, httpx.TimeoutException):
        reason = "連線逾時"
    elif isinstance(exc, httpx.ProxyError):
        reason = "代理伺服器拒絕連線"
    elif isinstance(exc, httpx.ConnectError):
        reason = "無法連線（請檢查這台機器能否上網、DNS 是否正常）"
    else:
        reason = "網路錯誤"
    detail = str(exc) or type(exc).__name__
    return f"連不到 {host or '外部服務'}：{reason}（{detail}）"


class GuardedClient(httpx.Client):
    """get/post 等都會經過 request；網路錯誤一律轉成 error_cls。"""

    def __init__(self, error_cls: Type[Exception], **kwargs):
        super().__init__(**kwargs)
        self._error_cls = error_cls

    def request(self, method, url, **kwargs) -> httpx.Response:
        try:
            return super().request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise self._error_cls(describe(exc)) from exc
