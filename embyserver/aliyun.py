"""阿里雲盤開放平台（openapi.alipan.com）：登入、列目錄、取檔案的 SHA1、讀檔案的一小段。給「從阿里雲盤秒傳到 115」用。

照 OpenList 的 aliyundrive_open 驅動；成功時的欄位沒實測，錯誤原文照實顯示。詳細說明見 docs/modules.md 的「embyserver/aliyun.py」。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import List, Optional, Tuple

import httpx

from .config import AliyunConfig
from .db import Database
from .http_util import GuardedClient, describe
from .p115 import _int

log = logging.getLogger(__name__)

OPENAPI = "https://openapi.alipan.com"
TOKEN_META_KEY = "aliyun_refresh_token"  # 和 115 的 cookie 一樣存在資料庫，不寫進設定檔
TOKEN_CODES = {"AccessTokenInvalid", "AccessTokenExpired", "I400JD"}  # 換一次 access token 再試
TOKEN_LIFE = 7200  # 回應沒給 expires_in 時當成幾秒
TOKEN_EARLY = 300  # 提早幾秒換
# 官方的頻率限制（照 OpenList）：列目錄每秒 4 次、取下載網址每秒 1 次、其他每秒 15 次；兩次之間至少隔幾秒
PACES = {"list": 0.26, "url": 1.1, "other": 0.07}
PAGE = 100
RANGE_TIMEOUT = 30
DRIVES = (("資源庫", "resource_drive_id"), ("備份盤", "default_drive_id"))  # 網頁上最上層的兩個虛擬資料夾
SHA1_RE = re.compile(r"[0-9A-Fa-f]{40}")


class AliyunError(Exception):
    pass


class _Pace:
    """兩次請求之間至少隔 gap 秒，執行緒安全：先在鎖裡排好自己的時間，再在鎖外等。"""

    def __init__(self, gap: float):
        self.gap = gap
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self.gap
        if wait > 0:
            time.sleep(wait)


def _entry(item: dict) -> dict:
    """openFile/list 的一項（沒實測）：缺欄位、型別不對都當成沒有。SHA1 只收 40 位十六進位、雜湊名稱是 sha1（或沒給）的。"""
    digest = str(item.get("content_hash") or "")
    algo = str(item.get("content_hash_name") or "sha1").lower()
    return {
        "id": str(item.get("file_id") or ""), "name": str(item.get("name") or ""),
        "is_dir": str(item.get("type") or "") == "folder", "size": _int(item.get("size")),
        "sha1": digest.upper() if algo == "sha1" and SHA1_RE.fullmatch(digest) else "",
    }


def _why(body: dict, resp: httpx.Response) -> str:
    return str(body.get("message") or body.get("text") or body.get("code") or f"HTTP {resp.status_code}")


class AliyunDrive:
    def __init__(self, db: Database, cfg: AliyunConfig, transport: Optional[httpx.BaseTransport] = None, timeout: float = 30.0):
        self.db = db
        self.cfg = cfg  # 和 Config 裡的是同一個物件：網頁上改了設定，下一個請求就用新的
        self._client = GuardedClient(AliyunError, timeout=timeout, follow_redirects=False, transport=transport)
        self._access = ""
        self._expires = 0.0
        self._token_lock = threading.Lock()  # 同時只換一次 token；登入、登出也拿它
        self._info: Optional[dict] = None  # getDriveInfo，換帳號才重讀
        self._paces = {k: _Pace(v) for k, v in PACES.items()}

    def close(self) -> None:
        self._client.close()

    # ---------------- 登入 ----------------

    @property
    def refresh_token(self) -> str:
        return self.db.get_meta(TOKEN_META_KEY) or ""

    @property
    def logged_in(self) -> bool:
        return bool(self.refresh_token)

    @property
    def own_client(self) -> bool:
        return bool(self.cfg.client_id.strip() and self.cfg.client_secret.strip())

    def login(self, refresh_token: str) -> dict:
        """存起 refresh token、換一次 access token、讀帳號資訊；不成功就換回原本的（原本沒登入就是清掉），丟 AliyunError。"""
        token = (refresh_token or "").strip()
        if not token:
            raise AliyunError("請貼上阿里雲盤的 refresh token")
        if not token.isascii() or any(c.isspace() for c in token):
            raise AliyunError("refresh token 裡有空白、換行或中文，請重新複製一次")
        with self._token_lock:
            old = self.refresh_token
            self.db.set_meta(TOKEN_META_KEY, token)
            self._access, self._expires, self._info = "", 0.0, None
        try:
            self._token()
            info = self.drive_info()
        except AliyunError:
            with self._token_lock:
                self.db.set_meta(TOKEN_META_KEY, old)
                self._access, self._expires, self._info = "", 0.0, None
            raise
        log.info("阿里雲盤登入：%s", info["name"] or info["user_id"])
        return self.status()

    def logout(self) -> None:
        with self._token_lock:
            self.db.set_meta(TOKEN_META_KEY, "")
            self._access, self._expires, self._info = "", 0.0, None
        log.info("阿里雲盤已登出")

    def status(self) -> dict:
        """{logged_in, name, drives: [資源庫、備份盤裡有的], own_client, error}；讀不到帳號資訊時 error 寫原因。"""
        out = {"logged_in": self.logged_in, "name": "", "drives": [], "own_client": self.own_client, "error": ""}
        if not out["logged_in"]:
            return out
        try:
            info = self.drive_info()
        except AliyunError as exc:
            return {**out, "error": str(exc)}
        return {**out, "name": info["name"], "drives": [d["name"] for d in info["drives"]]}

    def _token(self, stale: str = "") -> str:
        """目前的 access token；快到期、或 stale（剛被阿里雲盤說過期的那個）還是目前的，就換一次。
        換回來的 refresh token 會換新（舊的可能失效），一定存回資料庫。"""
        with self._token_lock:
            if self._access and self._access != stale and time.time() < self._expires - TOKEN_EARLY:
                return self._access
            refresh = self.refresh_token
            if not refresh:
                raise AliyunError("還沒登入阿里雲盤：到「115 網盤 → 阿里雲盤秒傳」貼上 refresh token")
            access, new_refresh, expires = self._exchange(refresh)
            self._access, self._expires = access, time.time() + (expires if expires > 0 else TOKEN_LIFE)
            if new_refresh and new_refresh != refresh:
                self.db.set_meta(TOKEN_META_KEY, new_refresh)
            return access

    def _exchange(self, refresh: str) -> Tuple[str, str, int]:
        """用 refresh token 換 (access token, 新的 refresh token, 幾秒後到期)。有自己的 client id 就向開放平台換；
        沒有就用線上 API（OpenList 的服務，會把 refresh token 送給它）。"""
        cid, secret = self.cfg.client_id.strip(), self.cfg.client_secret.strip()
        if cid and secret:
            resp = self._client.post(f"{OPENAPI}/oauth/access_token", json={
                "client_id": cid, "client_secret": secret, "grant_type": "refresh_token", "refresh_token": refresh})
            where = "阿里雲盤開放平台"
        else:
            api = self.cfg.online_api.strip()
            if not api:
                raise AliyunError("沒有填自己的 client id、client secret，也沒有線上換 token 的網址，換不了阿里雲盤的 access token")
            resp = self._client.get(api, params={"refresh_ui": refresh, "server_use": "true", "driver_txt": "alicloud_qr"})
            where = "線上換 token 的服務"
        body = self._json(resp)
        access = str(body.get("access_token") or "")
        if not access:
            raise AliyunError(f"{where}換不到 access token：{_why(body, resp)}（refresh token 過期的話，重新拿一個貼上）")
        return access, str(body.get("refresh_token") or ""), _int(body.get("expires_in"))

    @staticmethod
    def _json(resp: httpx.Response) -> dict:
        try:
            body = resp.json()
        except ValueError:
            raise AliyunError(f"阿里雲盤的回應不是 JSON：HTTP {resp.status_code}")
        if not isinstance(body, dict):
            raise AliyunError(f"阿里雲盤的回應格式看不懂：HTTP {resp.status_code}")
        return body

    # ---------------- 開放平台 API ----------------

    def _call(self, path: str, body: Optional[dict] = None, pace: str = "other") -> dict:
        """POST JSON。回 code 的就是錯：token 過期換一次再試一次；其他照「code：message」丟 AliyunError。"""
        for attempt in (1, 2):
            self._paces[pace].wait()
            token = self._token()
            resp = self._client.post(f"{OPENAPI}{path}", json=body or {}, headers={"Authorization": f"Bearer {token}"})
            data = self._json(resp)
            code = str(data.get("code") or "")
            if code in TOKEN_CODES and attempt == 1:
                self._token(stale=token)
                continue
            if code:
                raise AliyunError(f"阿里雲盤：{code}：{data.get('message') or ''}".rstrip("："))
            if resp.status_code >= 400:
                raise AliyunError(f"阿里雲盤回應 HTTP {resp.status_code}")
            return data
        raise AliyunError("阿里雲盤的 access token 換了還是不能用")  # 走不到：第二次一定 return 或 raise

    def drive_info(self) -> dict:
        """{user_id, name, drives: [{name, id}]}（資源庫、備份盤，帳號有的才列）；快取到換帳號為止。"""
        if self._info is None:
            data = self._call("/adrive/v1.0/user/getDriveInfo")
            drives = [{"name": label, "id": str(data.get(key) or "")} for label, key in DRIVES if data.get(key)]
            self._info = {"user_id": str(data.get("user_id") or ""), "name": str(data.get("name") or data.get("nick_name") or ""),
                          "drives": drives}
        return self._info

    def list_dir(self, drive_id: str, parent_id: str = "root") -> List[dict]:
        """一個資料夾直接底下的 [{id, name, is_dir, size, sha1}]，照名稱排；用 next_marker 翻頁。"""
        out: List[dict] = []
        marker, seen = "", set()
        while True:
            body = {"drive_id": drive_id, "parent_file_id": parent_id, "limit": PAGE, "order_by": "name",
                    "order_direction": "ASC"}
            if marker:
                body["marker"] = marker
            data = self._call("/adrive/v1.0/openFile/list", body, pace="list")
            items = data.get("items") if isinstance(data.get("items"), list) else []
            out += [e for e in (_entry(i) for i in items if isinstance(i, dict)) if e["id"] and e["name"]]
            seen.add(marker)
            marker = str(data.get("next_marker") or "")
            if not marker or not items or marker in seen:  # 同一個 marker 又出現：不要一直翻
                return out

    def download_url(self, drive_id: str, file_id: str) -> str:
        data = self._call("/adrive/v1.0/openFile/getDownloadUrl",
                          {"drive_id": drive_id, "file_id": file_id, "expire_sec": 900}, pace="url")
        url = str(data.get("url") or "")
        if not url.startswith(("https://", "http://")):
            raise AliyunError("阿里雲盤沒有給這支檔案的下載網址")
        return url

    def read_range(self, url: str, start: int, end: int, size: int = -1) -> bytes:
        """讀檔案的第 start 到 end 個位元組（end 含在內）。要回 206 而且長度剛好；從 0 讀到最後一個位元組時 200 也接受。
        先看狀態再讀內容：伺服器不理 Range、回整個檔案時不會讀進記憶體。"""
        want = end - start + 1
        if start < 0 or want <= 0:
            raise AliyunError(f"要讀的範圍不對：{start}-{end}")
        try:
            with self._client.stream("GET", url, headers={"Range": f"bytes={start}-{end}"}, timeout=RANGE_TIMEOUT,
                                     follow_redirects=True) as resp:
                whole = resp.status_code == 200 and start == 0 and end == size - 1
                if resp.status_code != 206 and not whole:
                    raise AliyunError(f"阿里雲盤讀不到第 {start}-{end} 個位元組：HTTP {resp.status_code}")
                data = bytearray()
                for chunk in resp.iter_bytes():
                    data += chunk
                    if len(data) > want:
                        break
        except httpx.HTTPError as exc:  # stream 不經過 GuardedClient.request，自己轉
            raise AliyunError(describe(exc)) from exc
        if len(data) != want:
            raise AliyunError(f"阿里雲盤第 {start}-{end} 個位元組讀到 {len(data)} 個，長度不對")
        return bytes(data)

    # ---------------- 路徑 ----------------

    def resolve(self, path: str) -> dict:
        """/資源庫/電影/某部片 → {drive_id, id, name, is_dir, size, sha1, path}；一層一層列目錄找名稱
        （不用 get_by_path，它沒實測過）。最上層是虛擬的「資源庫」「備份盤」。"""
        parts = [p for p in str(path or "").split("/") if p]
        if not parts:
            raise AliyunError("請選阿里雲盤上的一個資料夾或檔案")
        drives = self.drive_info()["drives"]
        drive = next((d for d in drives if d["name"] == parts[0]), None)
        if not drive:
            names = "、".join(d["name"] for d in drives) or "（沒有）"
            raise AliyunError(f"阿里雲盤上沒有「{parts[0]}」：最上層是 {names}")
        node = {"id": "root", "name": parts[0], "is_dir": True, "size": 0, "sha1": ""}
        for i, name in enumerate(parts[1:], 2):
            if not node["is_dir"]:
                raise AliyunError(f"阿里雲盤上的 /{'/'.join(parts[:i - 1])} 是檔案，不是資料夾")
            node = next((e for e in self.list_dir(drive["id"], node["id"]) if e["name"] == name), None)
            if node is None:
                raise AliyunError(f"阿里雲盤上找不到 /{'/'.join(parts[:i])}")
        return {**node, "drive_id": drive["id"], "path": "/" + "/".join(parts)}

    def dirs(self, path: str) -> dict:
        """選資料夾的對話框：這一層的子資料夾名稱和檔案數。最上層（空的或 /）是資源庫、備份盤。"""
        parts = [p for p in str(path or "").split("/") if p]
        if not parts:
            return {"path": "/", "parent": None, "dirs": [d["name"] for d in self.drive_info()["drives"]], "files": 0}
        node = self.resolve(path)
        if not node["is_dir"]:
            raise AliyunError(f"{node['path']} 是檔案，不是資料夾")
        entries = self.list_dir(node["drive_id"], node["id"])
        return {"path": node["path"], "parent": "/" + "/".join(parts[:-1]),
                "dirs": [e["name"] for e in entries if e["is_dir"]], "files": sum(1 for e in entries if not e["is_dir"])}

    def walk(self, drive_id: str, folder_id: str, limit: int, halted=lambda: False) -> List[dict]:
        """資料夾底下（含子資料夾）的檔案 [{id, name, size, sha1, dir（相對的資料夾，最上層是 ""）}]，照名稱排；
        湊到 limit 支就停（呼叫的人看長度知道超過了沒有）。halted() 為真時停在兩個資料夾之間，回傳列到的（呼叫的人自己再看）。"""
        out: List[dict] = []
        queue: List[Tuple[str, str]] = [(folder_id, "")]
        while queue and len(out) < limit and not halted():
            fid, rel = queue.pop(0)
            for e in self.list_dir(drive_id, fid):
                if e["is_dir"]:
                    queue.append((e["id"], f"{rel}/{e['name']}" if rel else e["name"]))
                else:
                    out.append({**e, "dir": rel})
        return out[:limit]
