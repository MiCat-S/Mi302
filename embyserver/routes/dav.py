"""/dav/ 的 WebDAV 路由（只能讀）。規則在 webdav.py。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from ..p115 import P115Error
from ..webdav import DavError, content_type
from .common import state

router = APIRouter()

READ = ["OPTIONS", "PROPFIND", "GET", "HEAD"]
WRITE = ["PUT", "DELETE", "MKCOL", "COPY", "MOVE", "PROPPATCH", "LOCK", "UNLOCK", "POST"]
ALLOW = ", ".join(READ)
CHALLENGE = {"WWW-Authenticate": 'Basic realm="Mi302 WebDAV", charset="UTF-8"'}


def _text(status: int, message: str, headers: dict = None) -> Response:
    return Response(message, status_code=status, media_type="text/plain; charset=utf-8", headers=headers)


# 同步函式：FastAPI 放到執行緒裡跑（驗密碼、向 115 要清單都會等）
@router.api_route("/dav", methods=READ + WRITE)
@router.api_route("/dav/{path:path}", methods=READ + WRITE)
def dav(request: Request, path: str = "") -> Response:
    dav = state(request).webdav
    if not dav.cfg.enabled:
        return _text(404, "WebDAV 沒有開啟（設定 → WebDAV）")
    method = request.method
    if method == "OPTIONS":
        return Response(status_code=200, headers={"DAV": "1", "Allow": ALLOW, "MS-Author-Via": "DAV"})
    user = dav.login(request.headers.get("authorization", ""))
    if not user:
        return _text(401, "要用 Mi302 的帳號密碼登入", CHALLENGE)
    if dav.cfg.admin_only and not user["is_admin"]:
        return _text(403, "WebDAV 設定成只讓管理員登入")
    if method not in READ:
        return _text(405, "Mi302 的 WebDAV 只能讀", {"Allow": ALLOW})
    try:
        node = dav.resolve(path)
        if method == "PROPFIND":
            depth = request.headers.get("depth", "1")
            return Response(dav.propfind(node, depth), status_code=207, media_type='application/xml; charset="utf-8"')
        if node["kind"] != "file":
            if not request.url.path.endswith("/"):  # 資料夾一律用 / 結尾，裡面的相對連結才對
                return RedirectResponse(dav.href(node["path"], True), status_code=301)
            return HTMLResponse(dav.index(node))
        entry = node["entry"]
        if method == "HEAD":  # 大小從清單就知道，不必向 115 要直鏈
            return Response(status_code=200, headers={
                "Content-Length": str(entry["size"]), "Accept-Ranges": "bytes",
                "Content-Type": content_type(entry["name"])})
        url = dav.p115.download_url(entry["pickcode"], request.headers.get("user-agent", ""))
    except DavError as exc:
        return _text(exc.status, str(exc))
    except P115Error as exc:
        return _text(502, f"讀不到 115：{exc}")
    return RedirectResponse(url, status_code=302)
