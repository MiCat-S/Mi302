"""從阿里雲盤秒傳到 115：阿里雲盤的檔案清單直接給 SHA1，115 有同一個檔案就秒傳過來（不真的上傳，115 沒有的列出來）；
秒傳完的資料夾加進「整理 115 網盤」，選了「整理到」就交給 MoviePilot 整理（和轉存分享一樣）。

115 的上傳初始化沒有文件，照 OpenList、p115client 的做法：第一步實測過，二次驗證（status 7）和秒傳成功（status 2）沒實測，
錯誤照實顯示。詳細說明見 docs/modules.md 的「embyserver/rapid115.py」。
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import httpx

from . import deletelog
from .aliyun import AliyunError
from .filetypes import SUBTITLE_EXTS, VIDEO_EXTS
from .organize115 import OrganizeError, _dir_path
from .p115 import P115Error, P115Throttled, _int
from .share115 import folder_name
from .workers import Workers

import p115cipher  # noqa: E402  在 .p115 之後：它替 Python 3.10 補好 p115cipher 用到的 int.from_bytes 預設值

log = logging.getLogger(__name__)

APPVER_API = "https://appversion.115.com/1/web/1.0/api/chrome"  # data.win.version_code
UPLOAD_INFO_API = "https://proapi.115.com/app/uploadinfo"  # user_id、userkey、size_limit
UPLOAD_INIT_API = "https://uplb.115.com/4.0/initupload.php"
APPVER_FALLBACK = "36.0.1"  # 讀不到版本號時用（2026-10-03 的）
APPVER_TTL = 6 * 3600
INFO_TTL = 3600
PACE = 1.0  # 兩次 115 請求之間至少隔幾秒，115 限流剛恢復時再乘上 slowdown
MAX_FILES = 5000  # 一次最多秒傳幾支，超過就不做（選小一點的資料夾）
MAX_RESULTS = 2000  # 每一支的結果最多記幾筆（數量照算）
PREVIEW = 200  # 讀取時列出前幾支
LISTING_TTL = 600  # 讀取時列過的檔案清單，幾秒內開始秒傳就不重列阿里雲盤
OK, MISSING, FAILED, SKIPPED = "ok", "missing", "failed", "skipped"
SIGN_RANGE_RE = re.compile(r"\s*(\d+)\s*-\s*(\d+)\s*")
VERSION_RE = re.compile(r"\d+(?:\.\d+){1,3}")


class RapidError(Exception):
    pass


def _path(value: str) -> str:
    return "/" + "/".join(p for p in str(value or "").split("/") if p)


def _kind(name: str) -> str:
    ext = posixpath.splitext(name)[1].lower()
    return "video" if ext in VIDEO_EXTS else "subtitle" if ext in SUBTITLE_EXTS else "other"


def _field(body: dict, key: str):
    """上傳初始化的回應（沒實測的部分照 OpenList）：欄位在最上層，沒有再看 data 裡。"""
    if key in body:
        return body[key]
    data = body.get("data")
    return data.get(key) if isinstance(data, dict) else None


def _message(body: dict) -> str:
    return str(_field(body, "statusmsg") or _field(body, "message") or _field(body, "error") or "").strip()


def _decode(resp: httpx.Response) -> dict:
    """回應是加密的（ecdh_aes_decrypt 解開是 JSON）；解不開再當成一般 JSON（出錯時 115 可能直接回明文）。"""
    try:
        body = json.loads(p115cipher.ecdh_aes_decrypt(resp.content))
    except Exception:
        try:
            body = resp.json()
        except ValueError:
            raise P115Error(f"115 秒傳的回應看不懂：HTTP {resp.status_code}")
    if not isinstance(body, dict):
        raise P115Error("115 秒傳的回應格式看不懂")
    return body


@dataclass
class RapidJob:
    """秒傳一個阿里雲盤的資料夾（或一支檔案）：在伺服器上一支一支做（關掉網頁也會做完），可以停（做完手上這一支）。
    results 每一支：path（相對於來源）、size、state（ok／missing／failed／skipped）、message；最多 MAX_RESULTS 筆。
    skipped 是沒有 SHA1、超過 115 單檔上限的；ignored 是只要影片和字幕時，其他的檔案（不傳，也不列）。"""

    running: bool = False
    stopping: bool = False
    stopped: bool = False
    started: float = 0.0
    finished: float = 0.0
    error: str = ""
    current: str = ""
    source: str = ""
    folder: str = ""  # 115 上新建的那個資料夾路徑
    target: str = ""  # path：整理到 target_path；空的：先不整理
    target_path: str = ""
    total: int = 0
    done: int = 0
    ok: int = 0
    missing: int = 0
    failed: int = 0
    skipped: int = 0
    ignored: int = 0
    results: List[dict] = field(default_factory=list)
    unit_id: str = ""
    organizing: bool = False
    note: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["results_total"] = self.ok + self.missing + self.failed + self.skipped
        return d


class RapidUploader:
    def __init__(self, organizer, strm_sync, aliyun):
        self.organizer = organizer
        self.strm_sync = strm_sync
        self.aliyun = aliyun
        self.workers = Workers()  # 程式結束時停在兩支之間
        self.job = RapidJob()
        self._starting = threading.Lock()  # 同時只跑一批
        self.pace = PACE  # 測試裡調小
        self._last = 0.0  # 上一次 115 請求（monotonic）
        self._version: Tuple[str, float] = ("", 0.0)
        self._info: Optional[Tuple[str, dict, float]] = None  # (cookie, 上傳資訊, 讀到的時間)：換 cookie 就作廢
        self._seen: Optional[dict] = None  # 讀取時列過的：{source, at, node, files}

    @property
    def p115(self):
        return self.strm_sync.p115  # 和「整理 115 網盤」用同一個（理由同 ShareTransfer）

    @property
    def db(self):
        return self.organizer.db

    def stop(self) -> None:
        """程式要結束：在等的地方馬上醒來，停在兩支之間。"""
        self.workers.stop.set()

    def cancel(self) -> bool:
        """按了停止：做完手上這一支就停；秒傳好的加進整理，不交給 MoviePilot。沒在秒傳回傳 False。"""
        if not self.job.running:
            return False
        self.job.stopping = True
        return True

    def status(self) -> dict:
        return self.job.as_dict()

    def _halted(self) -> bool:
        return self.workers.stop.is_set() or self.job.stopping

    # ---------------- 115 秒傳 ----------------

    def app_version(self, fresh: bool = False) -> str:
        """115 瀏覽器的版本號（上傳初始化的 User-Agent 和 appversion 要用最新的，不然回「请升级到最新版本」）。
        快取 APPVER_TTL 秒；讀不到用上次的，再不行用 APPVER_FALLBACK。"""
        version, at = self._version
        if version and not fresh and time.time() - at < APPVER_TTL:
            return version
        code = ""
        try:
            data = self.p115._client.get(APPVER_API).json()
            win = (data.get("data") or {}).get("win") if isinstance(data, dict) else None
            code = str(win.get("version_code") or "") if isinstance(win, dict) else ""
        except (P115Error, ValueError, AttributeError) as exc:
            log.info("讀不到 115 瀏覽器的版本號：%s", exc)
        if VERSION_RE.fullmatch(code):
            self._version = (code, time.time())
            return code
        return version or APPVER_FALLBACK

    def upload_info(self) -> dict:
        """{user_id, userkey, size_limit（單檔上限，0 = 不檢查）, note}；用 cookie 讀，快取 INFO_TTL 秒，換 cookie 就重讀。"""
        cookies = self.p115.cookies
        if not cookies:
            raise P115Error("秒傳要用掃碼登入（cookie）")
        cached = self._info
        if cached and cached[0] == cookies and time.time() - cached[2] < INFO_TTL:
            return cached[1]
        self.p115.breaker.check()
        body = self.p115._api_json(self.p115._client.get(UPLOAD_INFO_API, headers=self.p115._cookie_headers()))
        userkey, user_id = str(body.get("userkey") or ""), _int(body.get("user_id"))
        if not body.get("state", True) or not userkey or not user_id:
            why = body.get("error") or body.get("message") or body.get("upload_allowed_msg") or body
            raise P115Error(f"讀不到 115 的上傳資訊：{why}")
        note = "" if body.get("upload_allowed", True) else str(body.get("upload_allowed_msg") or "115 說這個帳號現在不能上傳")
        info = {"user_id": user_id, "userkey": userkey, "size_limit": max(0, _int(body.get("size_limit"))), "note": note}
        self._info = (cookies, info, time.time())
        return info

    def _init(self, payload: dict, version: str) -> dict:
        """送一次上傳初始化。make_upload_payload 會在傳進去的 dict 加 t、sig、token，所以每次給它一份新的。"""
        p115 = self.p115
        p115.breaker.check()
        kw = p115cipher.make_upload_payload(dict(payload))
        resp = p115._client.post(UPLOAD_INIT_API, params=kw["params"], content=kw["data"], headers={
            "Cookie": p115.cookies, "User-Agent": f"Mozilla/5.0 115Browser/{version}",
            "Content-Type": "application/x-www-form-urlencoded"})
        if resp.status_code in (405, 429):
            p115.breaker.inspect(status=resp.status_code)
            raise P115Throttled(p115.breaker.message())
        if resp.status_code != 200:
            raise P115Error(f"115 秒傳回應 HTTP {resp.status_code}")
        body = _decode(resp)
        if body.get("state") is False and p115.breaker.inspect(data=body):  # 登入失效、限流的寫法和其他 webapi 一樣時
            raise P115Throttled(p115.breaker.message())
        return body

    def rapid_upload(self, name: str, size: int, sha1: str, cid: int, read_range: Callable[[int, int], bytes]) -> dict:
        """請 115 秒傳一支檔案到 cid：{state: ok／missing／failed, message, pickcode}。
        read_range(起, 迄) 讀原檔那一段（迄含在內），115 要二次驗證時才呼叫。"""
        info = self.upload_info()
        version = self.app_version()
        payload = {"appid": 0, "appversion": version, "behavior_type": 0, "sign_key": "", "sign_val": "", "topupload": 0,
                   "userid": info["user_id"], "userkey": info["userkey"], "filename": name, "fileid": sha1.upper(),
                   "filesize": size, "target": f"U_1_{cid}"}
        body = self._init(payload, version)
        if _int(_field(body, "status")) == 4 and "升级" in _message(body):  # 版本號舊了：不用快取重讀一次再送
            version = self.app_version(fresh=True)
            payload["appversion"] = version
            body = self._init(payload, version)
        if _int(_field(body, "status")) == 7:
            body = self._verify(payload, version, body, size, read_range)
        status = _int(_field(body, "status"))
        if status == 2:
            return {"state": OK, "message": "", "pickcode": str(_field(body, "pickcode") or "")}
        if status == 1:
            return {"state": MISSING, "message": "115 上沒有這個檔案，不能秒傳", "pickcode": ""}
        why = _message(body) or f"status {_field(body, 'status')}、statuscode {_field(body, 'statuscode')}"
        return {"state": FAILED, "message": f"115：{why}", "pickcode": ""}

    def _verify(self, payload: dict, version: str, body: dict, size: int, read_range: Callable[[int, int], bytes]) -> dict:
        """二次驗證（沒實測，照 OpenList）：sign_check 是「起-迄」（迄含在內），讀原檔那一段算 SHA1（大寫十六進位）當 sign_val，
        帶 sign_key 再送一次。115 對秒傳有時限，拖太久會回 sig invalid，所以讀完馬上送、中間不等。"""
        check, key = str(_field(body, "sign_check") or ""), str(_field(body, "sign_key") or "")
        m = SIGN_RANGE_RE.fullmatch(check)
        if not m or not key:
            raise P115Error(f"115 要二次驗證，但看不懂它給的範圍：{check or '（沒有）'}")
        start, end = int(m.group(1)), int(m.group(2))
        if end < start or size and end >= size:
            raise P115Error(f"115 要驗證第 {start}-{end} 個位元組，超出檔案大小 {size}")
        data = read_range(start, end)
        if len(data) != end - start + 1:
            raise AliyunError(f"阿里雲盤第 {start}-{end} 個位元組讀到 {len(data)} 個，長度不對")
        return self._init({**payload, "sign_key": key, "sign_val": hashlib.sha1(data).hexdigest().upper()}, version)

    # ---------------- 讀取、開始 ----------------

    def _collect(self, source: str, halted: Callable[[], bool]) -> Tuple[dict, List[dict]]:
        """來源（資料夾或一支檔案）底下的檔案，最多 MAX_FILES + 1 支（多一支才知道超過了）；讀取時列過、LISTING_TTL 秒內的直接用。"""
        seen = self._seen
        if seen and seen["source"] == source and time.time() - seen["at"] < LISTING_TTL:
            return seen["node"], seen["files"]
        node = self.aliyun.resolve(source)
        if node["is_dir"]:
            files = self.aliyun.walk(node["drive_id"], node["id"], MAX_FILES + 1, halted)
        else:
            files = [{**node, "dir": ""}]
        files = [{**f, "drive_id": node["drive_id"]} for f in files]
        if not halted():  # 沒列完的不留
            self._seen = {"source": source, "at": time.time(), "node": node, "files": files}
        return node, files

    def read(self, source: str, media_only: bool = True) -> dict:
        """列出要秒傳的：幾支影片、字幕、其他、總大小、沒有 SHA1 的、超過 115 單檔上限的，和前 PREVIEW 支。"""
        if not self.aliyun.logged_in:
            raise RapidError("還沒登入阿里雲盤：先在上面貼 refresh token 登入")
        source = _path(source)
        if source == "/":
            raise RapidError("請選阿里雲盤上的一個資料夾或檔案")
        node, files = self._collect(source, self.workers.stop.is_set)
        limit, note = 0, ""
        try:
            info = self.upload_info()
            limit, note = info["size_limit"], info["note"]
        except P115Error as exc:
            note = f"讀不到 115 的單檔上限（{exc}）"
        todo = [f for f in files[:MAX_FILES] if not media_only or _kind(f["name"]) != "other"]
        rows = [{"path": f"{f['dir']}/{f['name']}" if f["dir"] else f["name"], "size": f["size"], "kind": _kind(f["name"]),
                 "skip": _skip(f, limit)} for f in todo]
        kinds = [r["kind"] for r in rows]
        return {"source": node["path"], "name": node["name"], "is_dir": node["is_dir"], "total": len(rows),
                "videos": kinds.count("video"), "subtitles": kinds.count("subtitle"), "others": kinds.count("other"),
                "ignored": len(files[:MAX_FILES]) - len(rows), "size": sum(r["size"] for r in rows),
                "no_sha1": sum(1 for f in todo if not f["sha1"]), "too_big": sum(1 for f in todo if limit and f["size"] > limit),
                "size_limit": limit, "too_many": len(files) > MAX_FILES, "max_files": MAX_FILES, "items": rows[:PREVIEW],
                "more": max(0, len(rows) - PREVIEW), "note": note}

    def start(self, source: str, folder: str, target: str, target_path: str, media_only: bool = True) -> dict:
        """在背景秒傳：source 是阿里雲盤路徑（/資源庫/…），folder 是存到的 115 資料夾（要已經存在），底下建一個新資料夾放；
        target 是 path（整理到 target_path）或空的（先不整理）。回傳 job。"""
        if not self.p115.cookies:
            raise RapidError("秒傳要用掃碼登入（cookie）")
        if not self.aliyun.logged_in:
            raise RapidError("還沒登入阿里雲盤：先在上面貼 refresh token 登入")
        source, folder = _path(source), _dir_path(folder)
        if source == "/":
            raise RapidError("請選阿里雲盤上的一個資料夾或檔案")
        if folder == "/":
            raise RapidError("請選要存到哪個 115 資料夾（不能是最上層）")
        if target not in ("path", ""):
            raise RapidError("「整理到」要是一個 115 資料夾，或先不整理")
        target_path = _dir_path(target_path) if target == "path" else ""
        if target_path == "/":
            raise RapidError("請填要整理到哪個 115 資料夾")
        if target and not self.organizer.mp.enabled:
            raise RapidError("還沒設定 MoviePilot，「整理到」只能選先不整理")
        if self.job.running:  # 先擋一次，免得白問 115
            raise RapidError("已經在秒傳了，等它做完")
        if self.p115.breaker.tripped:
            raise RapidError(self.p115.breaker.message())
        try:
            cid = self.p115.dir_id(folder)
        except P115Throttled as exc:
            raise RapidError(str(exc))
        except P115Error as exc:
            raise RapidError(f"找不到 115 資料夾 {folder}：{exc}（請先在 115 建好）")
        if not cid:
            raise RapidError(f"找不到 115 資料夾 {folder}")
        with self._starting:
            if self.job.running:
                raise RapidError("已經在秒傳了，等它做完")
            self.job = RapidJob(running=True, started=time.time(), source=source, target=target, target_path=target_path)
        self.workers.start(self._run, cid, folder, bool(media_only))
        log.info("從阿里雲盤秒傳 %s 到 115 的 %s，%s", source, folder, f"整理到 {target_path}" if target else "先不整理")
        return self.job.as_dict()

    # ---------------- 背景 ----------------

    def _run(self, folder_cid: int, folder: str, media_only: bool) -> None:
        job = self.job
        try:
            job.current = "列出阿里雲盤上的檔案"
            node, files = self._collect(job.source, self._halted)
            if self._halted():
                job.stopped = job.stopping
                job.error = "" if job.stopping else "程式要結束，沒列完"
                return
            if len(files) > MAX_FILES:
                job.error = f"超過 {MAX_FILES} 支檔案，這次不做：選小一點的資料夾"
                return
            todo = [f for f in files if not media_only or _kind(f["name"]) != "other"]
            job.total, job.ignored = len(todo), len(files) - len(todo)
            if not todo:
                job.error = "沒有要秒傳的檔案" + ("（只要影片和字幕）" if media_only and files else "")
                return
            limit = self.upload_info()["size_limit"]
            root_cid, job.folder = self._make_root(folder_cid, folder, node)
            self._upload_all(job, todo, root_cid, limit)
            job.stopped = job.stopped or job.stopping  # 最後一支做到一半才按停止：沒有沒做的，也要寫「按了停止」
            self._finish(job, root_cid)
            log.info("從阿里雲盤秒傳：%s 支成功，%s 支 115 沒有，%s 支失敗，%s 支略過", job.ok, job.missing, job.failed, job.skipped)
        except P115Throttled as exc:  # 熔斷：整批停下，不再碰 115（也不加進整理：那要列 115 的目錄）
            job.error = str(exc)
            if job.folder:
                job.note = f"秒傳好的 {job.ok} 支在 {job.folder}；115 恢復之後到「瀏覽 115」把它加進整理"
        except (AliyunError, P115Error, RapidError) as exc:
            job.error = str(exc)
        except Exception as exc:  # 背景執行緒：記下來，不讓網頁一直顯示「秒傳中」
            job.error = f"{type(exc).__name__}: {exc}"
            log.exception("從阿里雲盤秒傳時發生錯誤")
        finally:
            job.current = ""
            job.running = False
            job.finished = time.time()

    def _gap(self) -> None:
        """每個 115 請求之前：熔斷中丟 P115Throttled；和上一次至少隔 pace 秒（限流剛恢復時放慢），程式要結束就不等了。"""
        self.p115.breaker.check()
        wait = self._last + self.pace * self.p115.breaker.slowdown() - time.monotonic()
        if wait > 0:
            self.workers.stop.wait(wait)
        self._last = time.monotonic()

    def _make_root(self, folder_cid: int, folder: str, node: dict) -> Tuple[int, str]:
        """在「存到」底下建放這次秒傳的資料夾：名稱是來源的名稱（一支檔案時去掉副檔名），同名的已經有了就加「 (2)」「 (3)」…。"""
        base = node["name"] if node["is_dir"] else posixpath.splitext(node["name"])[0]
        base = folder_name(base, "阿里雲盤秒傳")
        self._gap()
        taken = {e["name"] for e in self.p115.list_dir(folder_cid)}
        name, n = base, 2
        while name in taken:
            name, n = f"{base} ({n})", n + 1
        path = posixpath.join(folder, name)
        self._gap()
        return self.p115.make_dir(folder_cid, name, path), path

    def _dir(self, rel: str, dirs: Dict[str, Tuple[int, str]]) -> int:
        """來源的子資料夾在 115 上照樣建（相對路徑 → (cid, 115 路徑) 記在 dirs，不重建）。"""
        if rel not in dirs:
            parent_rel, name = posixpath.split(rel)
            pcid = self._dir(parent_rel, dirs)
            name = folder_name(name, "未命名")
            path = f"{dirs[parent_rel][1]}/{name}"
            self._gap()
            try:
                cid = self.p115.make_dir(pcid, name, path)
            except P115Throttled:
                raise
            except P115Error:  # 多半是同名的已經有了（去掉 115 不收的字元之後撞名）：用那一個
                cid = self.p115.dir_id(path)
            dirs[rel] = (cid, path)
        return dirs[rel][0]

    def _upload_all(self, job: RapidJob, todo: List[dict], root_cid: int, limit: int) -> None:
        dirs: Dict[str, Tuple[int, str]] = {"": (root_cid, job.folder)}
        for f in todo:
            if self._halted():
                job.stopped = job.stopping
                return
            rel = f"{f['dir']}/{f['name']}" if f["dir"] else f["name"]
            job.current = rel
            why = _skip(f, limit)
            if why:
                self._record(job, rel, f["size"], SKIPPED, why)
                continue
            try:
                cid = self._dir(f["dir"], dirs)
                self._gap()
                res = self.rapid_upload(f["name"], f["size"], f["sha1"], cid, self._reader(f))
            except P115Throttled:
                raise
            except (AliyunError, P115Error) as exc:
                res = {"state": FAILED, "message": str(exc)}
            self._record(job, rel, f["size"], res["state"], res["message"])

    def _reader(self, f: dict) -> Callable[[int, int], bytes]:
        """115 要二次驗證時才向阿里雲盤取下載網址（同一支只取一次），讀那一段。"""
        url: List[str] = []

        def read(start: int, end: int) -> bytes:
            if not url:
                url.append(self.aliyun.download_url(f["drive_id"], f["id"]))
            return self.aliyun.read_range(url[0], start, end, f["size"])

        return read

    @staticmethod
    def _record(job: RapidJob, path: str, size: int, state: str, message: str) -> None:
        setattr(job, state, getattr(job, state) + 1)
        job.done += 1
        if len(job.results) < MAX_RESULTS:
            job.results.append({"path": path, "size": size, "state": state, "message": message})

    def _finish(self, job: RapidJob, root_cid: int) -> None:
        """做完：一支都沒成功就把剛建的空資料夾移到回收站；有成功的加進「整理 115 網盤」，選了整理到就交給 MoviePilot。"""
        if self.workers.stop.is_set():
            job.note = f"程式要結束：秒傳好的 {job.ok} 支在 {job.folder}，沒有加進整理，到「瀏覽 115」加進整理"
            return
        if not job.ok:
            job.note = self._drop_empty(root_cid, job.folder)
            return
        try:
            job.unit_id = str(self.organizer.folder_unit(root_cid, job.folder)["id"])
        except OrganizeError as exc:
            job.note = f"秒傳好的在 {job.folder}，但加不進「整理 115 網盤」：{exc}"
            return
        if not job.target:
            job.note = "已加進「整理 115 網盤」"
        elif job.stopping:
            job.note = "已加進「整理 115 網盤」；按了停止，沒有交給 MoviePilot 整理"
        else:
            batch = self.organizer.organize_units_in_background([job.unit_id], "path", job.target_path, cleanup=True)
            job.organizing = batch is not None
            job.note = (f"已交給 MoviePilot 整理到 {job.target_path}：到「整理 115 網盤」看結果" if batch else
                        "已加進「整理 115 網盤」；目前有別的整理在跑，等它做完再按「全部整理」")

    def _drop_empty(self, root_cid: int, root: str) -> str:
        """一支都沒成功：剛建的資料夾（含子資料夾）裡真的沒有檔案，才移到 115 回收站、寫刪除紀錄。"""
        try:
            self._gap()  # 熔斷中不再列 115
            if any(not e["is_dir"] for _, e in self.p115.walk(root_cid, delay=self.pace)):
                return f"一支都沒秒傳成功；{root} 裡有別的檔案，留著沒刪"
            self._gap()
            self.p115.delete_files([root_cid])
        except P115Error as exc:
            return f"一支都沒秒傳成功；建好的 {root} 沒移到回收站（{exc}），空的話到「瀏覽 115」刪掉"
        deletelog.record(self.db, "rapid", [{"file_id": root_cid, "path": root, "is_dir": True}])
        return f"一支都沒秒傳成功，建好的空資料夾 {root} 已經移到 115 回收站"


def _skip(f: dict, limit: int) -> str:
    if not f["sha1"]:
        return "阿里雲盤沒有給這支的 SHA1，不能秒傳"
    if limit and f["size"] > limit:
        return "超過 115 的單檔上限"
    return ""
