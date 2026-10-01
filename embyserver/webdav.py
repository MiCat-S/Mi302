"""/dav/ 的 WebDAV（只能讀）：Infuse、VidHub、Kodi 這類播放器直接瀏覽 115，播放時 302 到 115 直鏈，不必產生 strm。

詳細說明見 docs/modules.md 的「embyserver/webdav.py」。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import posixpath
import threading
import time
from email.utils import formatdate
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote
from xml.sax.saxutils import escape

from .p115 import P115Error, P115NotFound
from .strm_sync import remote_root

LIST_TTL = 120  # 資料夾內容快取幾秒
# 路徑 → 資料夾 id 記幾秒：不能比上一層的清單久，否則移出露出範圍的資料夾，舊路徑還能靠記住的 id 列下去
ID_TTL = LIST_TTL
ID_LIMIT = 20000
AUTH_TTL = 300  # 驗過的帳號密碼記幾秒
PREFIX = "/dav"


class DavError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def norm(path: str) -> str:
    """/dav 後面的路徑轉成 115 路徑：去掉多餘的斜線、. 和 ..（不會跑出根目錄）。"""
    p = posixpath.normpath("/" + (path or "").strip("/"))
    return "/" if p in ("", ".", "//") else p


def _inside(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root.rstrip("/") + "/")


class WebDAV:
    def __init__(self, config, auth, p115, strm_sync):
        self.config = config  # 整份 Config：webdav 那一節網頁上改了馬上生效
        self.auth = auth
        self.p115 = p115
        self.strm_sync = strm_sync
        self._lists: Dict[int, Tuple[float, List[dict]]] = {}  # 資料夾 id → (時間, 內容)
        self._ids: Dict[str, Tuple[float, int]] = {}  # 115 路徑 → (時間, 資料夾 id)，從上一層的清單拿到的
        self._users: Dict[str, Tuple[float, dict]] = {}  # Authorization 的雜湊 → (時間, 使用者)
        self._lock = threading.Lock()

    @property
    def cfg(self):
        return self.config.webdav

    # ---------------- 登入 ----------------

    def login(self, header: str) -> Optional[dict]:
        """HTTP Basic 登入，回傳使用者；帳號密碼不對回傳 None。
        記住的登入每次還是查一下使用者：刪掉了、改了密碼的馬上失效。"""
        if not header.lower().startswith("basic "):
            return None
        key = hashlib.sha256(header.encode()).hexdigest()
        now = time.time()
        with self._lock:
            hit = self._users.get(key)
        if hit and now - hit[0] < AUTH_TTL:
            user = self.auth.get_user(hit[1]["id"])
            if user and user["password_hash"] == hit[1]["password_hash"]:
                return user
        try:
            name, _, password = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
        user = self.auth.authenticate(name, password)
        with self._lock:
            self._users = {k: v for k, v in self._users.items() if now - v[0] < AUTH_TTL and k != key}
            if user:
                self._users[key] = (now, user)
        return user

    # ---------------- 範圍 ----------------

    def roots(self) -> List[str]:
        """露出的 115 資料夾；互相包含的只留外層。"""
        found = [norm(self.cfg.root)] if self.cfg.root else [norm(remote_root(t)) for t in self.strm_sync.tasks]
        out: List[str] = []
        for r in sorted(set(found), key=len):
            if not any(_inside(r, o) for o in out):
                out.append(r)
        return out

    def _virtual(self, path: str, roots: List[str]) -> List[str]:
        """path 是某個露出的資料夾的上層時，回傳通往它們的下一層名稱；不是的話回傳空的。"""
        names = []
        for r in roots:
            if r != path and _inside(r, path):
                rest = r[len(path.rstrip("/")) + 1:]
                names.append(rest.split("/", 1)[0])
        return sorted(set(names))

    # ---------------- 115 ----------------

    def listing(self, cid: int, path: str) -> List[dict]:
        now = time.time()
        with self._lock:
            hit = self._lists.get(cid)
        if hit and now - hit[0] < LIST_TTL:
            return hit[1]
        entries = self.p115.list_dir(cid)
        with self._lock:
            self._lists = {k: v for k, v in self._lists.items() if now - v[0] < LIST_TTL}
            self._lists[cid] = (now, entries)
            if len(self._ids) > ID_LIMIT:
                self._ids = {k: v for k, v in self._ids.items() if now - v[0] < ID_TTL}
            here = {posixpath.join(path, e["name"]) for e in entries if e["is_dir"]}
            prefix = path.rstrip("/") + "/"
            gone = [k for k in self._ids if k.startswith(prefix) and "/" not in k[len(prefix):] and k not in here]
            if gone:  # 不在這一層了（改名、搬走、刪掉）：它和底下記住的 id 都不算數
                self._ids = {k: v for k, v in self._ids.items() if not any(_inside(k, g) for g in gone)}
            for e in entries:  # 子資料夾的 id 記下來，往下走不必每層再列一次
                if e["is_dir"]:
                    self._ids[posixpath.join(path, e["name"])] = (now, e["id"])
        return entries

    def _dir_id(self, path: str, roots: List[str]) -> int:
        if path == "/":
            return 0
        with self._lock:
            hit = self._ids.get(path)
        if hit and time.time() - hit[0] < ID_TTL:
            return hit[1]
        if path in roots:
            cid = self.p115.dir_id(path)
            with self._lock:
                self._ids[path] = (time.time(), cid)
            return cid
        entry = self._entry(path, roots)
        if not entry["is_dir"]:
            raise DavError(404, "不是資料夾")
        return entry["id"]

    def _entry(self, path: str, roots: List[str]) -> dict:
        """露出範圍裡的一個檔案或資料夾（從上一層的清單找）。"""
        parent = posixpath.dirname(path)
        for e in self.listing(self._dir_id(parent, roots), parent):
            if e["name"] == posixpath.basename(path):
                return e
        raise DavError(404, "找不到")

    def resolve(self, raw: str) -> dict:
        """回傳 {kind: virtual|dir|file, path, name, children（virtual）, cid（dir）, entry（file）}；範圍外的 404。"""
        path = norm(raw)
        roots = self.roots()
        if not roots:
            raise DavError(404, "沒有同步任務，也沒有設定 WebDAV 要露出的 115 資料夾")
        try:
            if not any(_inside(path, r) for r in roots):
                children = self._virtual(path, roots)
                if not children:
                    raise DavError(404, "不在 WebDAV 露出的範圍裡")
                return {"kind": "virtual", "path": path, "children": children}
            if path in roots:
                return {"kind": "dir", "path": path, "cid": self._dir_id(path, roots)}
            entry = self._entry(path, roots)
        except P115NotFound:
            self.forget(path)
            raise DavError(404, "115 上沒有這個資料夾")
        except P115Error as exc:
            raise DavError(502, f"讀不到 115：{exc}")
        if entry["is_dir"]:
            return {"kind": "dir", "path": path, "cid": entry["id"]}
        return {"kind": "file", "path": path, "entry": entry}

    def children(self, node: dict) -> List[dict]:
        """資料夾裡的東西，給 PROPFIND Depth 1 和 GET 資料夾用。"""
        if node["kind"] == "virtual":
            return [{"name": n, "is_dir": True, "size": 0, "mtime": 0} for n in node["children"]]
        try:
            return self.listing(node["cid"], node["path"])
        except P115NotFound:
            self.forget(node["path"])
            raise DavError(404, "115 上沒有這個資料夾")
        except P115Error as exc:
            raise DavError(502, f"讀不到 115：{exc}")

    def forget(self, path: str) -> None:
        """這個路徑和底下記住的資料夾 id 都不算數了。"""
        with self._lock:
            self._ids = {k: v for k, v in self._ids.items() if not _inside(k, path)}

    # ---------------- 回應 ----------------

    @staticmethod
    def href(path: str, is_dir: bool) -> str:
        h = quote(PREFIX + ("" if path == "/" else path))
        return h + "/" if is_dir else h

    def propfind(self, node: dict, depth: str) -> str:
        """207 Multi-Status：自己，Depth 不是 0 時再加一層裡面的（infinity 也只給一層，避免一次列整個 115）。"""
        me = {"name": posixpath.basename(node["path"]) or "Mi302", "is_dir": node["kind"] != "file",
              "size": node.get("entry", {}).get("size", 0), "mtime": node.get("entry", {}).get("mtime", 0)}
        parts = [_response(self.href(node["path"], me["is_dir"]), me)]
        if me["is_dir"] and depth != "0":
            for e in self.children(node):
                parts.append(_response(self.href(posixpath.join(node["path"], e["name"]), e["is_dir"]), e))
        return ('<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">'
                + "".join(parts) + "</D:multistatus>")

    def index(self, node: dict) -> str:
        """瀏覽器打開資料夾時的簡單清單（不是 WebDAV 的一部分，方便確認看得到）。"""
        rows = [f'<li><a href="{escape(self.href(posixpath.join(node["path"], e["name"]), e["is_dir"]))}">'
                f'{escape(e["name"])}{"/" if e["is_dir"] else ""}</a></li>' for e in self.children(node)]
        up = '<li><a href="../">../</a></li>' if node["path"] != "/" else ""
        return (f'<!doctype html><meta charset="utf-8"><title>{escape(node["path"])}</title>'
                f'<h1>{escape(node["path"])}</h1><ul>{up}{"".join(rows)}</ul>')


def content_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _response(href: str, e: dict) -> str:
    mtime = formatdate(e.get("mtime") or 0, usegmt=True)
    if e["is_dir"]:
        props = "<D:resourcetype><D:collection/></D:resourcetype>"
    else:
        ctype = content_type(e["name"])
        props = (f"<D:resourcetype/><D:getcontentlength>{int(e.get('size') or 0)}</D:getcontentlength>"
                 f"<D:getcontenttype>{escape(ctype)}</D:getcontenttype>")
    return (f"<D:response><D:href>{escape(href)}</D:href><D:propstat><D:prop>"
            f"<D:displayname>{escape(e['name'])}</D:displayname>{props}<D:getlastmodified>{mtime}</D:getlastmodified>"
            "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")
