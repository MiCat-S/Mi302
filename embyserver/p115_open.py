"""115 開放平台（open.115.com）通道。

與 cookie 通道的差別：用 OAuth PKCE 裝置碼授權取得 access_token（約 2 小時）與 refresh_token，
呼叫 proapi.115.com/open/* 官方授權介面，比較不容易被風控或被其他登入踢下線。
需要先在 115 開放平台申請應用，取得 AppID（client_id）。

授權流程：
1. POST passportapi.115.com/open/authDeviceCode（client_id + code_challenge）→ uid、time、sign
2. 用 uid 顯示二維碼，輪詢 qrcodeapi.115.com/get/status/
3. 確認後 POST passportapi.115.com/open/deviceCodeToToken（uid + code_verifier）→ token
4. 過期前 POST passportapi.115.com/open/refreshToken 續期
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import threading
import time
from typing import Dict, List, Optional

import httpx

from .db import Database

log = logging.getLogger(__name__)

OPEN_BASE = "https://proapi.115.com"
AUTH_DEVICE_CODE = "https://passportapi.115.com/open/authDeviceCode"
DEVICE_CODE_TO_TOKEN = "https://passportapi.115.com/open/deviceCodeToToken"
REFRESH_TOKEN = "https://passportapi.115.com/open/refreshToken"
QRCODE_STATUS = "https://qrcodeapi.115.com/get/status/"
QRCODE_IMAGE = "https://qrcodeapi.115.com/api/1.0/web/1.0/qrcode"
OPEN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TOKEN_META_KEY = "p115_open_token"
REFRESH_LEAD = 300  # 過期前 5 分鐘續期

# access_token 失效，刷新後重試
AUTH_EXPIRED_CODES = {40140124, 40140125, 40140126}
# refresh_token 失效，需要重新掃碼
REFRESH_DEAD_CODE = 40140116


class P115OpenError(Exception):
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


def _state_ok(value) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1")
    return False


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def parse_download_url(data) -> Optional[str]:
    """downurl 回傳格式不一：字串、{url: …}、{url: {url: …}}、{file_id: {url: {url: …}}}。"""
    if isinstance(data, str):
        return data if data.startswith("http") else None
    if isinstance(data, dict):
        url = data.get("url")
        if isinstance(url, str) and url.startswith("http"):
            return url
        if isinstance(url, dict) and str(url.get("url", "")).startswith("http"):
            return url["url"]
        for value in data.values():
            found = parse_download_url(value)
            if found:
                return found
    return None


class P115OpenClient:
    def __init__(self, db: Database, app_id: str = "", timeout: float = 15.0, transport=None):
        self.db = db
        self.default_app_id = app_id
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._sessions: Dict[str, tuple[str, str]] = {}  # uid -> (app_id, verifier)
        self._refresh_lock = threading.Lock()

    # ---------------- token ----------------

    def _load(self) -> Optional[dict]:
        raw = self.db.get_meta(TOKEN_META_KEY)
        return json.loads(raw) if raw else None

    def _save(self, token: Optional[dict]) -> None:
        self.db.set_meta(TOKEN_META_KEY, json.dumps(token) if token else "")

    @property
    def authorized(self) -> bool:
        token = self._load()
        return bool(token and token.get("refresh_token"))

    def status(self) -> dict:
        token = self._load() or {}
        return {
            "authorized": self.authorized,
            "app_id": token.get("app_id") or self.default_app_id,
            "expires_at": token.get("expires_at"),
        }

    def logout(self) -> None:
        self._save(None)

    def _store_token(self, app_id: str, data: dict, old_refresh: str = "") -> dict:
        token = {
            "app_id": app_id,
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or old_refresh,
            "expires_at": int(time.time()) + int(data.get("expires_in") or 7200),
        }
        self._save(token)
        return token

    def access_token(self, force_refresh: bool = False) -> str:
        token = self._load()
        if not token or not token.get("refresh_token"):
            raise P115OpenError("尚未授權 115 開放平台", REFRESH_DEAD_CODE)
        if not force_refresh and token.get("access_token") and token["expires_at"] - REFRESH_LEAD > time.time():
            return token["access_token"]
        with self._refresh_lock:
            # 等鎖期間可能已被其他執行緒刷新
            latest = self._load() or token
            if not force_refresh and latest.get("expires_at", 0) - REFRESH_LEAD > time.time():
                return latest["access_token"]
            if force_refresh and latest.get("access_token") != token.get("access_token"):
                return latest["access_token"]
            try:
                data = self._post_form(REFRESH_TOKEN, {"refresh_token": latest["refresh_token"]})
            except P115OpenError as exc:
                if exc.code == REFRESH_DEAD_CODE:
                    self._save(None)
                    raise P115OpenError("開放平台授權已失效，請重新掃碼", exc.code) from exc
                raise
            if not data.get("access_token"):
                raise P115OpenError("刷新 token 失敗：回應沒有 access_token")
            log.info("115 開放平台 token 已續期")
            return self._store_token(latest.get("app_id", ""), data, latest["refresh_token"])["access_token"]

    # ---------------- HTTP ----------------

    def _envelope(self, resp: httpx.Response):
        if resp.status_code == 401:
            raise P115OpenError("HTTP 401 未授權", 40140124)
        try:
            body = resp.json()
        except ValueError as exc:
            raise P115OpenError(f"115 開放平台回應不是 JSON：HTTP {resp.status_code}") from exc
        if not _state_ok(body.get("state")):
            raise P115OpenError(body.get("message") or body.get("error") or "未知錯誤", int(body.get("code") or 0))
        return body

    def _post_form(self, url: str, form: dict) -> dict:
        resp = self._client.post(url, data=form, headers={"User-Agent": OPEN_UA})
        return self._envelope(resp).get("data") or {}

    def _call(self, method: str, path: str, params=None, form=None, user_agent: str = "") -> dict:
        for attempt in range(2):
            token = self.access_token(force_refresh=attempt > 0)
            resp = self._client.request(
                method,
                OPEN_BASE + path,
                params=params,
                data=form,
                headers={"Authorization": f"Bearer {token}", "User-Agent": user_agent or OPEN_UA},
            )
            try:
                return self._envelope(resp)
            except P115OpenError as exc:
                if exc.code in AUTH_EXPIRED_CODES and attempt == 0:
                    continue
                raise
        raise P115OpenError("呼叫 115 開放平台失敗")

    # ---------------- 掃碼授權 ----------------

    def qrcode_start(self, app_id: str = "") -> dict:
        app_id = (app_id or self.default_app_id or (self._load() or {}).get("app_id") or "").strip()
        if not app_id:
            raise P115OpenError("請先填寫 115 開放平台的 AppID")
        verifier, challenge = _pkce_pair()
        data = self._post_form(
            AUTH_DEVICE_CODE,
            {"client_id": app_id, "code_challenge": challenge, "code_challenge_method": "sha256"},
        )
        uid = data.get("uid")
        if not uid:
            raise P115OpenError("取得授權二維碼失敗，AppID 可能無效")
        self._sessions[str(uid)] = (app_id, verifier)
        return {
            "uid": str(uid),
            "time": str(data.get("time", "")),
            "sign": str(data.get("sign", "")),
            "qrcode_image": f"{QRCODE_IMAGE}?uid={uid}",
        }

    def qrcode_status(self, uid: str, time_: str, sign: str) -> dict:
        resp = self._client.get(
            QRCODE_STATUS, params={"uid": uid, "time": time_, "sign": sign}, headers={"User-Agent": OPEN_UA}
        )
        try:
            code = (resp.json().get("data") or {}).get("status")
        except ValueError:
            return {"status": "waiting"}
        if code in (None, 0):
            return {"status": "waiting"}
        if code == 1:
            return {"status": "scanned"}
        if code == 2:
            session = self._sessions.pop(uid, None)
            if not session:
                raise P115OpenError("授權會話已失效，請重新產生二維碼")
            app_id, verifier = session
            data = self._post_form(DEVICE_CODE_TO_TOKEN, {"uid": uid, "code_verifier": verifier})
            if not data.get("access_token"):
                raise P115OpenError("換取 token 失敗：回應沒有 access_token")
            self._store_token(app_id, data)
            log.info("115 開放平台授權成功")
            return {"status": "success"}
        self._sessions.pop(uid, None)
        return {"status": "canceled" if code == -2 else "expired"}

    # ---------------- 檔案 ----------------

    def download_url(self, pickcode: str, user_agent: str = "") -> str:
        body = self._call("POST", "/open/ufile/downurl", form={"pick_code": pickcode}, user_agent=user_agent)
        url = parse_download_url(body.get("data"))
        if not url:
            raise P115OpenError(f"開放平台沒有回傳下載網址：{pickcode}")
        return url

    def dir_id(self, path: str) -> int:
        path = "/" + path.strip("/")
        if path == "/":
            return 0
        body = self._call("GET", "/open/folder/get_info", params={"path": path})
        data = body.get("data") or {}
        cid = int(data.get("file_id") or data.get("fid") or 0)
        if not cid:
            raise P115OpenError(f"115 上找不到目錄：{path}")
        return cid

    def list_dir(self, cid: int) -> List[dict]:
        out: List[dict] = []
        offset = 0
        while True:
            body = self._call(
                "GET", "/open/ufile/files",
                params={"cid": cid, "limit": 1000, "offset": offset, "show_dir": 1, "cur": 1},
            )
            items = body.get("data")
            count = body.get("count")
            if isinstance(items, dict):  # 部分回應把清單包在 data.list
                count = items.get("count", count)
                items = items.get("list")
            items = items or []
            # cid 失效時 115 會靜默回傳根目錄
            path = body.get("path") or []
            if cid != 0 and path and str(path[-1].get("cid", cid)) != str(cid):
                raise P115OpenError(f"115 目錄不存在：{cid}")
            for info in items:
                is_dir = str(info.get("fc", info.get("file_category", "1"))) == "0"
                out.append(
                    {
                        "name": info.get("fn") or info.get("file_name") or "",
                        "is_dir": is_dir,
                        "id": int(info.get("fid") or info.get("file_id") or 0),
                        "pickcode": info.get("pc") or info.get("pick_code") or info.get("pickcode") or "",
                        "size": int(info.get("fs") or info.get("s") or info.get("size") or 0),
                        "mtime": int(info.get("upt") or info.get("te") or 0),
                    }
                )
            offset += len(items)
            if not items or offset >= int(count or 0):
                break
        return out
