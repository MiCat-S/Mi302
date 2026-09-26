"""使用者、token 與 Emby 認證標頭解析。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import HTTPException, Request

from .db import Database

_AUTH_PAIR_RE = re.compile(r'(\w+)="([^"]*)"')


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"pbkdf2${salt}${digest.hex()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored:
        return password == ""
    try:
        _, salt, hexdigest = stored.split("$", 2)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return hmac.compare_digest(digest.hex(), hexdigest)


def parse_emby_authorization(value: str) -> Dict[str, str]:
    """解析 `MediaBrowser Client="x", Device="y", DeviceId="z", Version="1", Token="t"`。"""
    return {k.lower(): v for k, v in _AUTH_PAIR_RE.findall(value or "")}


def client_info(request: Request) -> Dict[str, str]:
    info: Dict[str, str] = {}
    for header in ("x-emby-authorization", "authorization"):
        value = request.headers.get(header)
        if value and ("mediabrowser" in value.lower() or "emby" in value.lower()):
            info.update(parse_emby_authorization(value))
            break
    for key, header in (
        ("client", "x-emby-client"),
        ("device", "x-emby-device-name"),
        ("deviceid", "x-emby-device-id"),
        ("version", "x-emby-client-version"),
    ):
        if request.headers.get(header) and not info.get(key):
            info[key] = request.headers[header]
    q = lower_query(request)
    for key, param in (
        ("client", "x-emby-client"),
        ("device", "x-emby-device-name"),
        ("deviceid", "x-emby-device-id"),
        ("version", "x-emby-client-version"),
    ):
        if q.get(param) and not info.get(key):
            info[key] = q[param]
    return info


def lower_query(request: Request) -> Dict[str, str]:
    """Emby 客戶端的查詢參數大小寫不一，統一轉小寫鍵。"""
    cached = getattr(request.state, "lower_query", None)
    if cached is None:
        cached = {k.lower(): v for k, v in request.query_params.items()}
        request.state.lower_query = cached
    return cached


def extract_token(request: Request) -> Optional[str]:
    for header in ("x-emby-token", "x-mediabrowser-token"):
        if request.headers.get(header):
            return request.headers[header]
    info = client_info(request)
    if info.get("token"):
        return info["token"]
    q = lower_query(request)
    for param in ("api_key", "x-emby-token", "apikey", "x-mediabrowser-token"):
        if q.get(param):
            return q[param]
    return None


@dataclass
class AuthContext:
    user: Optional[dict]
    token: Optional[str]
    via_api_key: bool = False  # 用 API 金鑰（而不是登入 token）進來的：給 MoviePilot 這類程式用，不能進管理網頁

    @property
    def user_id(self) -> Optional[str]:
        return self.user["id"] if self.user else None


API_KEYS_META_KEY = "api_keys"


class AuthService:
    def __init__(self, db: Database, api_keys: list[str]):
        self.db = db
        self.api_keys = api_keys  # 設定檔的 api_keys（同一個 list，設定檔重新讀取時跟著變）

    # ---- API 金鑰（給 MoviePilot 等工具用，網頁上建立） ----
    def list_api_keys(self) -> list[dict]:
        try:
            return json.loads(self.db.get_meta(API_KEYS_META_KEY) or "[]")
        except ValueError:
            return []

    def create_api_key(self, name: str) -> dict:
        entry = {"key": secrets.token_hex(16), "name": name.strip() or "API", "created": now_iso()}
        keys = self.list_api_keys() + [entry]
        self.db.set_meta(API_KEYS_META_KEY, json.dumps(keys, ensure_ascii=False))
        return entry

    def delete_api_key(self, key: str) -> None:
        keys = [k for k in self.list_api_keys() if k["key"] != key]
        self.db.set_meta(API_KEYS_META_KEY, json.dumps(keys, ensure_ascii=False))

    def _is_api_key(self, token: str) -> bool:
        return token in self.api_keys or any(k["key"] == token for k in self.list_api_keys())

    # ---- users ----
    def ensure_user(self, name: str, password: str, admin: bool) -> None:
        """設定檔裡的帳號：不存在才建立，之後以網頁上的修改為準。"""
        if self.db.one("SELECT id FROM users WHERE name=?", (name,)):
            return
        self.create_user(name, password, admin)

    def create_user(self, name: str, password: str, admin: bool) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("使用者名稱不可空白")
        if self.db.one("SELECT id FROM users WHERE lower(name)=lower(?)", (name,)):
            raise ValueError(f"使用者已存在：{name}")
        user_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO users(id, name, password_hash, is_admin) VALUES(?,?,?,?)",
            (user_id, name, hash_password(password) if password else "", int(admin)),
        )
        return self.get_user(user_id)

    def update_user(self, user_id: str, password: Optional[str] = None, admin: Optional[bool] = None) -> dict:
        user = self.get_user(user_id)
        if not user:
            raise KeyError(user_id)
        if admin is False and user["is_admin"] and self._admin_count() <= 1:
            raise ValueError("至少要保留一個管理員")
        if password is not None:
            self.db.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (hash_password(password) if password else "", user["id"]),
            )
        if admin is not None:
            self.db.execute("UPDATE users SET is_admin=? WHERE id=?", (int(admin), user["id"]))
        return self.get_user(user["id"])

    def delete_user(self, user_id: str) -> None:
        user = self.get_user(user_id)
        if not user:
            raise KeyError(user_id)
        if user["is_admin"] and self._admin_count() <= 1:
            raise ValueError("至少要保留一個管理員")
        self.db.execute("DELETE FROM tokens WHERE user_id=?", (user["id"],))
        self.db.execute("DELETE FROM user_data WHERE user_id=?", (user["id"],))
        self.db.execute("DELETE FROM users WHERE id=?", (user["id"],))

    def reset_password(self, name: str, password: str) -> None:
        """命令列忘記密碼用：帳號不存在時建立成管理員。"""
        row = self.db.one("SELECT id FROM users WHERE name=?", (name,))
        if row:
            self.update_user(row["id"], password=password)
        else:
            self.create_user(name, password, True)

    def _admin_count(self) -> int:
        return self.db.one("SELECT COUNT(*) AS c FROM users WHERE is_admin=1")["c"]

    def get_user(self, user_id: str) -> Optional[dict]:
        row = self.db.one("SELECT * FROM users WHERE lower(id)=lower(?)", (user_id,))
        return dict(row) if row else None

    def list_users(self) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM users ORDER BY name")]

    def authenticate(self, username: str, password: str) -> Optional[dict]:
        row = self.db.one("SELECT * FROM users WHERE name=?", (username,))
        if not row or not verify_password(password, row["password_hash"]):
            return None
        return dict(row)

    # ---- tokens ----
    def issue_token(self, user: dict, info: Dict[str, str]) -> str:
        token = secrets.token_hex(16)
        ts = now_iso()
        device_id = info.get("deviceid", "")
        if device_id:
            # 同一裝置重新登入時淘汰舊 token
            self.db.execute(
                "DELETE FROM tokens WHERE user_id=? AND device_id=?",
                (user["id"], device_id),
            )
        self.db.execute(
            "INSERT INTO tokens(token, user_id, device_id, device_name, client, version, created, last_used) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                token,
                user["id"],
                device_id,
                info.get("device", ""),
                info.get("client", ""),
                info.get("version", ""),
                ts,
                ts,
            ),
        )
        self.db.execute(
            "UPDATE users SET last_login=?, last_activity=? WHERE id=?",
            (ts, ts, user["id"]),
        )
        return token

    def revoke(self, token: str) -> None:
        self.db.execute("DELETE FROM tokens WHERE token=?", (token,))

    def resolve(self, request: Request) -> AuthContext:
        token = extract_token(request)
        if not token:
            return AuthContext(None, None)
        if self._is_api_key(token):
            admin = self.db.one("SELECT * FROM users WHERE is_admin=1 ORDER BY name LIMIT 1")
            return AuthContext(dict(admin) if admin else None, token, via_api_key=True)
        row = self.db.one(
            "SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",
            (token,),
        )
        if not row:
            return AuthContext(None, token)
        return AuthContext(dict(row), token)


def require_user(request: Request) -> AuthContext:
    ctx: AuthContext = request.app.state.auth.resolve(request)
    if not ctx.user:
        raise HTTPException(status_code=401, detail="Access token is invalid or expired.")
    return ctx


def require_admin(request: Request) -> AuthContext:
    ctx = require_user(request)
    if not ctx.user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin required")
    if ctx.via_api_key and request.scope["path"].startswith(("/web/api", "/p115/")):
        # 金鑰是給 MoviePilot 這類程式呼叫 Emby API 用的；設定、備份（含 115 登入）、帳號要用帳號登入才能碰
        raise HTTPException(status_code=403, detail="API 金鑰不能用在管理網頁，請用管理員帳號登入")
    return ctx
