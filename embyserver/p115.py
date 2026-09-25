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
LOGIN_DEVICES_API = f"{QRCODE_API}/app/1.0/web/1.0/login_log/login_devices"
DOWNLOAD_API = "http://proapi.115.com/android/2.0/ufile/download"
USER_INFO_API = "https://my.115.com/?ct=ajax&ac=nav"
ACCOUNT_API = "https://my.115.com/?ct=ajax&ac=get_user_aq"
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
LOGIN_META_KEY = "p115_login"  # 怎麼登入的：{"method": "qrcode"/"cookie"/"config", "app": ..., "at": 時間}
ACCOUNT_CACHE_SECONDS = 60


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
        self._account_cache: Optional[Tuple[dict, float]] = None
        if initial_cookies and not self.cookies:
            self.set_cookies(initial_cookies, source="config")

    # ---------------- cookie ----------------

    @property
    def cookies(self) -> str:
        return self.db.get_meta(COOKIE_META_KEY) or ""

    def set_cookies(self, cookies: str, source: str = "cookie") -> None:
        self.db.set_meta(COOKIE_META_KEY, cookies.strip())
        login = {"method": source, "at": int(time.time())} if cookies.strip() else {}
        if source == "qrcode":
            login["app"] = self.app
        self.db.set_meta(LOGIN_META_KEY, json.dumps(login))
        self._account_cache = None
        with self._cache_lock:
            self._cache.clear()

    def logout(self) -> None:
        self.set_cookies("")

    @property
    def login_record(self) -> dict:
        try:
            return json.loads(self.db.get_meta(LOGIN_META_KEY) or "{}")
        except ValueError:
            return {}

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

    def dir_path(self, cid: int) -> str:
        """目錄 id 轉成完整路徑，例如 /影視/電影；根目錄是 /。"""
        if not cid:
            return "/"
        return self._dispatch("查詢目錄路徑", lambda: self.open.dir_path(cid), lambda: self._cookie_dir_path(cid))

    def iter_changed_files(self, cid: int, since: float) -> Iterator[dict]:
        """cid 底下（含所有子目錄）修改時間不早於 since 的檔案，由新到舊。

        回傳 {name, id, parent_id, pickcode, size, mtime}。增量同步用它找出新增、改名、移入的檔案。
        """
        if self.open.authorized:
            try:
                yield from self.open.iter_changed_files(cid, since)
                return
            except (P115OpenError, httpx.HTTPError) as exc:
                if not self.cookies:
                    raise P115Error(f"開放平台列出新檔案失敗：{exc}") from exc
                log.warning("開放平台列出新檔案失敗，改用 cookie：%s", exc)
        if not self.cookies:
            raise P115Error("尚未登入 115")
        yield from self._cookie_changed_files(cid, since)

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

    # ---------------- 帳號狀態 ----------------

    def account_info(self, refresh: bool = False) -> dict:
        """網頁顯示用的完整帳號狀態：帳號、VIP、空間、登入方式與裝置、開放平台授權。

        查一次要打好幾個 115 API，預設快取一分鐘。
        """
        cached = self._account_cache
        if cached and not refresh and time.time() - cached[1] < ACCOUNT_CACHE_SECONDS:
            return cached[0]
        info: dict = {
            "logged_in": self.logged_in,
            "checked_at": int(time.time()),
            "login": self.login_record if self.cookies else {},
            "cookie": self._cookie_account() if self.cookies else None,
            "open": self._open_account() if self.open.authorized else None,
        }
        # 帳號資料以 cookie 為主，沒有 cookie 時用開放平台的
        main = info["cookie"] if info["cookie"] and info["cookie"].get("valid") else info["open"]
        info["account"] = {k: main.get(k) for k in ("user_id", "user_name", "avatar", "vip", "space")} if main else None
        self._account_cache = (info, time.time())
        return info

    def _cookie_headers(self) -> dict:
        return {"Cookie": self.cookies, "User-Agent": BROWSER_UA}

    def _cookie_account(self) -> dict:
        out: dict = {"valid": False}
        try:
            data = _json(self._client.get(ACCOUNT_API, headers=self._cookie_headers()))
        except P115Error as exc:
            out["error"] = str(exc)
            return out
        if not data.get("state") or not isinstance(data.get("data"), dict):
            out["error"] = data.get("message") or data.get("error") or "cookie 已失效，請重新掃碼登入"
            return out
        d = data["data"]
        vip = d.get("vip") if isinstance(d.get("vip"), dict) else {}
        face = d.get("face") if isinstance(d.get("face"), dict) else {}
        out.update(
            valid=True,
            user_id=str(d.get("uid") or d.get("user_id") or _cookie_uid(self.cookies) or ""),
            user_name=d.get("uname") or d.get("user_name") or "",
            avatar=face.get("face_m") or face.get("face_l") or face.get("face_s") or "",
            vip={
                "is_vip": bool(_int(vip.get("is_vip"))),
                "is_forever": bool(_int(vip.get("is_forever"))),
                "expire": vip.get("expire_str") or _date(vip.get("expire")),
                "level": vip.get("level_name") or vip.get("vip_name") or "",
            },
        )
        try:
            space = self._webapi_get("/files/index_info", {"count_space_nums": 0})
            out["space"] = parse_space((space.get("data") or {}).get("space_info"))
        except P115Error as exc:
            out["space"] = None
            out["space_error"] = str(exc)
        out["devices"] = self._login_devices()
        return out

    def _login_devices(self) -> Optional[List[dict]]:
        """目前登入這個帳號的裝置；格式不符預期時回傳 None，網頁就不顯示這一段。"""
        try:
            data = self._client.get(LOGIN_DEVICES_API, headers=self._cookie_headers()).json()
            items = (data.get("data") or {}).get("list") if data.get("state") else None
        except (P115Error, ValueError, AttributeError):
            return None
        if not isinstance(items, list):
            return None
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            out.append(
                {
                    "name": it.get("name") or it.get("device") or it.get("ssoent") or "",
                    "app": it.get("ssoent") or it.get("app") or "",
                    "ip": it.get("ip") or "",
                    "city": it.get("city") or "",
                    "time": _int(it.get("utime") or it.get("login_time") or it.get("time")) or None,
                    "current": bool(it.get("is_current")),
                }
            )
        return out

    def _open_account(self) -> dict:
        out: dict = {"valid": False, **self.open.status()}
        try:
            d = self.open.user_info()
        except (P115OpenError, httpx.HTTPError) as exc:
            out["error"] = str(exc)
            return out
        vip = d.get("vip_info") if isinstance(d.get("vip_info"), dict) else {}
        out.update(
            valid=True,
            user_id=str(d.get("user_id") or ""),
            user_name=d.get("user_name") or "",
            avatar=d.get("user_face_m") or d.get("user_face_l") or d.get("user_face_s") or "",
            vip={
                "is_vip": bool(vip.get("level_name")) and "非" not in str(vip.get("level_name")),
                "is_forever": "永久" in str(vip.get("level_name") or ""),
                "expire": _date(vip.get("expire")),
                "level": vip.get("level_name") or "",
            },
            space=parse_space(d.get("rt_space_info")),
        )
        return out

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
        self.set_cookies("; ".join(f"{k}={v}" for k, v in cookie.items() if k and v), source="qrcode")
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

    def _cookie_dir_path(self, cid: int) -> str:
        data = self._webapi_get(
            "/files", {"cid": cid, "limit": 1, "show_dir": 1, "cur": 1, "aid": 1, "record_open_time": 0}
        )
        return path_from_ancestors(data.get("path"), cid)

    def _cookie_changed_files(self, cid: int, since: float) -> Iterator[dict]:
        offset = 0
        while True:
            data = self._webapi_get(
                "/files",
                {
                    "cid": cid, "cur": 0, "show_dir": 0, "o": "user_utime", "asc": 0, "custom_order": 2,
                    "limit": LIST_PAGE_SIZE, "offset": offset, "aid": 1, "count_folders": 0,
                    "record_open_time": 0,
                },
            )
            items = data.get("data") or []
            newer = 0
            for info in items:
                if "fid" not in info:
                    continue
                mtime = _int(info.get("te") or info.get("tp"))
                if mtime < since:
                    continue
                newer += 1
                yield {
                    "name": info.get("n") or "",
                    "id": int(info["fid"]),
                    "parent_id": int(info.get("cid") or 0),
                    "pickcode": info.get("pc") or "",
                    "size": _int(info.get("s")),
                    "mtime": mtime,
                }
            offset += len(items)
            # 依修改時間由新到舊排序，整頁都比 since 舊就不用再往下翻
            if not items or not newer or offset >= _int(data.get("count")):
                return

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
        from p115cipher import rsa_encrypt

        payload = json.dumps({"pick_code": pickcode}, separators=(",", ":")).encode()
        resp = self._client.post(
            DOWNLOAD_API,
            data={"data": rsa_encrypt(payload).decode()},
            headers={"User-Agent": user_agent, "Cookie": self.cookies},
        )
        data = _json(resp)
        if not data.get("state"):
            raise P115Error(f"115 取直鏈失敗：{data.get('error') or data.get('msg') or data}")
        try:
            detail = json.loads(rsa_decrypt(data["data"]))
        except (ValueError, TypeError, KeyError) as exc:
            raise P115Error(f"無法解析 115 回傳的直鏈：{exc}") from exc
        url = detail.get("url")
        if isinstance(url, dict):
            url = url.get("url")
        if not url:
            raise P115Error(f"115 回傳內容沒有網址：{detail}")
        return url


def rsa_decrypt(cipher_data) -> bytes:
    """解開 115 回傳的 RSA 加密內容。

    p115cipher 0.0.5.x 的 rsa_decrypt 對反轉後的 memoryview 再做 cast，會丟出
    「memoryview: casts are restricted to C-contiguous views」，所以這裡照同樣的演算法自己做，
    反轉前先轉成 bytes。
    """
    from base64 import b64decode

    from p115cipher import RSA_KEY
    from p115cipher.util import rsa_decrypt_with_pubkey, rsa_gen_key, xor

    data = bytes(rsa_decrypt_with_pubkey(b64decode(cipher_data)))
    key_l = rsa_gen_key(data[:16], 12)
    tmp = bytes(xor(data[16:], key_l))[::-1]
    return bytes(xor(tmp, RSA_KEY))


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _date(ts) -> str:
    ts = _int(ts)
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts > 0 else ""


def _cookie_uid(cookies: str) -> str:
    m = re.search(r"(?:^|;\s*)UID=(\d+)", cookies or "")
    return m.group(1) if m else ""


def parse_space(raw) -> Optional[dict]:
    """115 的空間資訊 {all_total: {size, size_format}, all_use, all_remain} 轉成位元組數。"""
    if not isinstance(raw, dict):
        return None

    def size(key):
        v = raw.get(key)
        return _int(v.get("size") if isinstance(v, dict) else v)

    total, used, remain = size("all_total"), size("all_use"), size("all_remain")
    if not total and not used:
        return None
    return {"total": total, "used": used, "remain": remain or max(total - used, 0)}


def path_from_ancestors(ancestors, cid: int) -> str:
    """115 列目錄回應裡的 path（由根到自己的祖先清單）轉成路徑字串。"""
    if not isinstance(ancestors, list) or not ancestors:
        raise P115Error(f"115 沒有回傳目錄 {cid} 的路徑")
    last = ancestors[-1]
    last_id = last.get("cid", last.get("file_id"))
    if str(last_id) != str(cid):
        raise P115Error(f"115 目錄不存在：{cid}")
    names = [
        str(a.get("name") or a.get("file_name") or "")
        for a in ancestors
        if str(a.get("cid", a.get("file_id", 0))) != "0"
    ]
    return "/" + "/".join(n for n in names if n)


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
