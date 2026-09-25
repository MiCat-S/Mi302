"""115 網盤：掃碼登入與依 pickcode 取下載直鏈。

流程參考 DDSRem-Dev/MoviePilot-Plugins 的 p115strmhelper：
- 掃碼登入：qrcodeapi.115.com 取 token → 輪詢狀態 → 換取 cookie
- 取直鏈：proapi.115.com/android/2.0/ufile/download，請求與回應以 115 的 RSA 方案加解密。
  直鏈綁定 User-Agent，所以要用播放器自己的 UA 去取，並依 (pickcode, UA) 快取。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, parse_qsl, unquote, urlsplit

import httpx

from .db import Database
from .http_util import GuardedClient
from .p115_open import P115OpenClient, P115OpenError

log = logging.getLogger(__name__)

QRCODE_API = "https://qrcodeapi.115.com"
DOWNLOAD_API = "http://proapi.115.com/android/2.0/ufile/download"
USER_INFO_API = "https://my.115.com/?ct=ajax&ac=nav"
WEBAPI = "https://webapi.115.com"
# webapi 需要像瀏覽器的 UA，否則容易被擋
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 115Browser/27.0"
)
LIST_PAGE_SIZE = 1150
PICKCODE_RE = re.compile(r"^[a-zA-Z0-9]{17}$")
SHORT_LINK_RE = re.compile(r"/d/([a-zA-Z0-9]{17})(?:\.[A-Za-z0-9]{1,5})?(?:/[^/]*)?$")
COOKIE_META_KEY = "p115_cookies"


class P115Error(Exception):
    pass


def extract_pickcode(url: str) -> Optional[str]:
    """從 strm 內容取出 pickcode。

    本伺服器產生的格式是 `/d/{pickcode}.mkv`；另外也認得其他工具產生的 strm：
    `.../d/{pickcode}`（115-station 等）、`?pickcode=xxx`（P115StrmHelper 等）、`115://xxx`。
    """
    if not url:
        return None
    m = SHORT_LINK_RE.search(url.split("?", 1)[0])
    if m:
        return m.group(1).lower()
    if url.startswith("115://"):
        code = url[len("115://"):].split("/", 1)[0].split("?", 1)[0]
        return code.lower() if PICKCODE_RE.match(code) else None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    values = parse_qs(parts.query).get("pickcode") or parse_qs(parts.query).get("pick_code")
    if values and PICKCODE_RE.match(values[0]):
        return values[0].lower()
    return None


class P115Service:
    def __init__(
        self,
        db: Database,
        initial_cookies: str = "",
        app: str = "alipaymini",
        timeout: float = 15.0,
        transport: Optional[httpx.BaseTransport] = None,
        open_app_id: str = "",
    ):
        self.db = db
        self.open = P115OpenClient(db, open_app_id, timeout, transport=transport)
        self.app = app
        self._client = GuardedClient(P115Error, timeout=timeout, follow_redirects=False, transport=transport)
        self._cache: Dict[Tuple[str, str], Tuple[str, float]] = {}
        self._cache_lock = threading.Lock()
        self._key_locks: Dict[Tuple[str, str], threading.Lock] = {}
        if initial_cookies and not self.cookies:
            self.set_cookies(initial_cookies)

    # ---------------- cookie ----------------

    @property
    def cookies(self) -> str:
        return self.db.get_meta(COOKIE_META_KEY) or ""

    def set_cookies(self, cookies: str) -> None:
        self.db.set_meta(COOKIE_META_KEY, cookies.strip())
        with self._cache_lock:
            self._cache.clear()

    def logout(self) -> None:
        self.set_cookies("")

    @property
    def logged_in(self) -> bool:
        return bool(self.cookies) or self.open.authorized

    # ---------------- 通道分派：開放平台優先，cookie 備援 ----------------

    def _dispatch(self, action: str, open_call, cookie_call):
        if self.open.authorized:
            try:
                return open_call()
            except (P115OpenError, httpx.HTTPError) as exc:
                if not self.cookies:
                    raise P115Error(f"開放平台{action}失敗：{exc}") from exc
                log.warning("開放平台%s失敗，改用 cookie：%s", action, exc)
        if not self.cookies:
            raise P115Error("尚未登入 115")
        return cookie_call()

    def dir_id(self, path: str) -> int:
        return self._dispatch("查詢目錄", lambda: self.open.dir_id(path), lambda: self._cookie_dir_id(path))

    def list_dir(self, cid: int) -> List[dict]:
        return self._dispatch("列目錄", lambda: self.open.list_dir(cid), lambda: self._cookie_list_dir(cid))

    def _fetch_download_url(self, pickcode: str, user_agent: str) -> str:
        return self._dispatch(
            "取直鏈",
            lambda: self.open.download_url(pickcode, user_agent),
            lambda: self._cookie_download_url(pickcode, user_agent),
        )

    def user_info(self) -> Optional[dict]:
        """用目前的 cookie 取得 115 帳號資訊，cookie 失效時回傳 None。"""
        if not self.cookies:
            return None
        try:
            resp = self._client.get(USER_INFO_API, headers={"Cookie": self.cookies})
            data = resp.json()
        except Exception:
            log.warning("取得 115 帳號資訊失敗", exc_info=True)
            return None
        if not data.get("state"):
            return None
        info = data.get("data") or {}
        return {"user_id": info.get("user_id"), "user_name": info.get("user_name"), "vip": info.get("vip")}

    # ---------------- 掃碼登入 ----------------

    def qrcode_token(self) -> dict:
        resp = self._client.get(f"{QRCODE_API}/api/1.0/web/1.0/token/")
        data = _json(resp)
        info = data.get("data") or {}
        if not (info.get("uid") and info.get("time") and info.get("sign")):
            raise P115Error(f"取得二維碼失敗：{data}")
        return {
            "uid": str(info["uid"]),
            "time": str(info["time"]),
            "sign": str(info["sign"]),
            "qrcode_image": f"{QRCODE_API}/api/1.0/web/1.0/qrcode?uid={info['uid']}",
        }

    def qrcode_status(self, uid: str, time_: str, sign: str) -> dict:
        """回傳 status：waiting / scanned / success / expired / canceled。成功時保存 cookie。"""
        resp = self._client.get(
            f"{QRCODE_API}/get/status/", params={"uid": uid, "time": time_, "sign": sign}
        )
        data = _json(resp)
        code = (data.get("data") or {}).get("status")
        if code in (None, 0):
            if data.get("message") == "key invalid":
                return {"status": "expired"}
            return {"status": "waiting"}
        if code == 1:
            return {"status": "scanned"}
        if code == -1:
            return {"status": "expired"}
        if code == -2:
            return {"status": "canceled"}
        if code == 2:
            self._finish_login(uid)
            return {"status": "success", "user": self.user_info()}
        return {"status": "unknown", "code": code}

    def _finish_login(self, uid: str) -> None:
        resp = self._client.post(
            f"{QRCODE_API}/app/1.0/{self.app}/1.0/login/qrcode/", data={"account": uid}
        )
        data = _json(resp)
        cookie = (data.get("data") or {}).get("cookie")
        if not data.get("state") or not isinstance(cookie, dict):
            raise P115Error(f"換取 cookie 失敗：{data.get('message') or data.get('error') or data}")
        self.set_cookies("; ".join(f"{k}={v}" for k, v in cookie.items() if k and v))
        log.info("115 掃碼登入成功")

    # ---------------- 目錄 ----------------

    def _webapi_get(self, path: str, params: dict) -> dict:
        if not self.cookies:
            raise P115Error("尚未登入 115")
        resp = self._client.get(
            f"{WEBAPI}{path}", params=params, headers={"Cookie": self.cookies, "User-Agent": BROWSER_UA}
        )
        data = _json(resp)
        if not data.get("state", True) and "data" not in data:
            raise P115Error(f"115 webapi 錯誤：{data.get('error') or data.get('errNo') or data}")
        return data

    def _cookie_dir_id(self, path: str) -> int:
        """由 115 路徑取目錄 id；根目錄為 0。"""
        path = "/" + path.strip("/")
        if path == "/":
            return 0
        data = self._webapi_get("/files/getid", {"path": path})
        cid = int(data.get("id") or 0)
        if cid == 0:
            raise P115Error(f"115 上找不到目錄：{path}")
        return cid

    def _cookie_list_dir(self, cid: int) -> List[dict]:
        """列出目錄的直接子項，回傳 {name, is_dir, id, pickcode, size, mtime}。"""
        out: List[dict] = []
        offset = 0
        while True:
            data = self._webapi_get(
                "/files",
                {
                    "cid": cid, "limit": LIST_PAGE_SIZE, "offset": offset, "show_dir": 1,
                    "cur": 1, "aid": 1, "count_folders": 1, "record_open_time": 0,
                },
            )
            # cid 不存在時 115 會回傳根目錄，要擋掉
            if cid != 0 and str((data.get("path") or [{}])[-1].get("cid", cid)) != str(cid):
                raise P115Error(f"115 目錄不存在：{cid}")
            items = data.get("data") or []
            for info in items:
                is_dir = "fid" not in info
                out.append(
                    {
                        "name": info.get("n") or "",
                        "is_dir": is_dir,
                        "id": int(info["cid"] if is_dir else info["fid"]),
                        "pickcode": info.get("pc") or "",
                        "size": int(info.get("s") or 0),
                        "mtime": int(info.get("te") or info.get("t") or 0),
                    }
                )
            offset += len(items)
            if not items or offset >= int(data.get("count") or 0):
                break
        return out

    def walk(self, cid: int, rel: str = "", delay: float = 0.0) -> Iterator[Tuple[str, dict]]:
        """遞迴遍歷，產生 (相對路徑, 檔案資訊)。"""
        for entry in self.list_dir(cid):
            child = f"{rel}/{entry['name']}" if rel else entry["name"]
            if entry["is_dir"]:
                if delay:
                    time.sleep(delay)
                yield from self.walk(entry["id"], child, delay)
            else:
                yield child, entry

    # ---------------- 下載直鏈 ----------------

    def download_url(self, pickcode: str, user_agent: str = "") -> str:
        if not self.logged_in:
            raise P115Error("尚未登入 115")
        pickcode = pickcode.lower()
        key = (pickcode, user_agent or "NoUA")
        cached = self._cached(key)
        if cached:
            return cached
        with self._cache_lock:
            lock = self._key_locks.setdefault(key, threading.Lock())
        # 同一檔案同一 UA 的並發請求只向 115 取一次
        with lock:
            cached = self._cached(key)
            if cached:
                return cached
            url = self._fetch_download_url(pickcode, user_agent)
            expires = _expire_ts(url)
            with self._cache_lock:
                now = time.time()
                self._cache = {k: v for k, v in self._cache.items() if v[1] > now}
                self._cache[key] = (url, expires)
                self._key_locks.pop(key, None)
            log.info("從 115 取得直鏈：%s %s", pickcode, unquote(urlsplit(url).path.rpartition("/")[-1]))
            return url

    def _cached(self, key: Tuple[str, str]) -> Optional[str]:
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and hit[1] > time.time():
                return hit[0]
        return None

    def _cookie_download_url(self, pickcode: str, user_agent: str) -> str:
        from p115cipher import rsa_decrypt, rsa_encrypt

        payload = json.dumps({"pick_code": pickcode}, separators=(",", ":")).encode()
        resp = self._client.post(
            DOWNLOAD_API,
            data={"data": rsa_encrypt(payload).decode()},
            headers={"User-Agent": user_agent, "Cookie": self.cookies},
        )
        data = _json(resp)
        if not data.get("state"):
            raise P115Error(f"115 取直鏈失敗：{data.get('error') or data.get('msg') or data}")
        detail = json.loads(rsa_decrypt(data["data"]))
        url = detail.get("url")
        if isinstance(url, dict):
            url = url.get("url")
        if not url:
            raise P115Error(f"115 回傳內容沒有網址：{detail}")
        return url


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError as exc:
        raise P115Error(f"115 回應不是 JSON：HTTP {resp.status_code}") from exc
    if not isinstance(data, dict):
        raise P115Error(f"115 回應格式異常：{data}")
    return data


def _expire_ts(url: str) -> float:
    """115 直鏈的 t 參數是到期時間，提前 5 分鐘失效；取不到就快取 10 分鐘。"""
    for k, v in parse_qsl(urlsplit(url).query):
        if k == "t" and v.isdigit():
            return int(v) - 300
    return time.time() + 600
