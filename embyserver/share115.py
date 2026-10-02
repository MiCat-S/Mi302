"""轉存 115 分享：讀出別人分享的內容、轉存到自己的 115（每個分享一個子資料夾），再加進「整理 115 網盤」交給 MoviePilot 整理。

用的是 115 網頁版沒有文件的 webapi（share/snap、share/receive、files/add、category/get，照開源的 p115client、OpenList），
115 改了介面就會失敗，錯誤訊息照實顯示。詳細說明見 docs/modules.md 的「embyserver/share115.py」。
"""

from __future__ import annotations

import logging
import posixpath
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from .organize115 import MAX_PINNED, OrganizeError, _dir_path, _inside
from .p115 import P115Error, P115Throttled, _int
from .strm_sync import remote_root
from .workers import Workers

log = logging.getLogger(__name__)

PACE = 1.0  # 兩個分享之間（讀、轉存）、讀分享翻頁時至少隔幾秒；115 限流剛恢復時再乘上 slowdown
POLL = 3.0  # 等 115 轉存完：幾秒看一次暫存資料夾
SETTLE = 10.0  # 勾的項目都出現之後，隔幾秒再讀一次資料夾裡的檔案數，兩次一樣才算轉存完
WAIT_MAX = 600  # 最多等 115 轉存幾秒
SNAP_PAGE = 32  # 讀分享時一次列幾項（p115client 的預設；115 接受的上限沒實測，寧可多翻幾頁）
SNAP_MAX = 1000  # 一個分享最上層最多列幾項
NAME_MAX = 100  # 暫存子資料夾的名稱最多幾個字
# 分享連結：115.com、115cdn.com、anxia.com 的 /s/分享碼；訪問碼在網址的 password=，或同一行的「訪問碼／提取碼／密碼」後面
SHARE_RE = re.compile(r"(?<![0-9A-Za-z-])(?:115|115cdn|anxia)\.com/s/([0-9A-Za-z]+)", re.I)
PASSWORD_RE = re.compile(r"[?&#]password=([0-9A-Za-z]{4})(?![0-9A-Za-z])")
CODE_WORD_RE = re.compile(r"(?:访问码|提取码|密码|訪問碼|提取碼|密碼)\s*[:：]?\s*([0-9A-Za-z]{4})(?![0-9A-Za-z])")
CODE_RE = re.compile(r"[0-9A-Za-z]{1,64}")
BAD_NAME_RE = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')  # 115 資料夾名稱不收的字元
EDGE_RE = re.compile(r"^[\s.]+|[\s.]+$")
WAITING, SKIPPED, FAILED, PINNED = "waiting", "skipped", "failed", "pinned"
PINNED_NOTE = "已加進「整理 115 網盤」"


class ShareError(Exception):
    pass


def parse_links(text: str) -> Tuple[List[dict], List[str]]:
    """貼上的內容：一行一個分享。回傳 ([{code, receive_code}], 看不懂的行)；同一個分享碼只留一個。
    沒有訪問碼的照樣送（有些分享不用）。"""
    shares: Dict[str, dict] = {}
    rejected: List[str] = []
    for line in re.split(r"[\r\n]+", text or ""):
        line = line.strip()
        if not line:
            continue
        m = SHARE_RE.search(line)
        if not m:
            rejected.append(line)
            continue
        pw = PASSWORD_RE.search(line) or CODE_WORD_RE.search(line)
        code, receive = m.group(1), pw.group(1) if pw else ""
        if code not in shares:
            shares[code] = {"code": code, "receive_code": receive}
        elif receive and not shares[code]["receive_code"]:  # 同一個貼了兩次，後面那行才寫訪問碼
            shares[code]["receive_code"] = receive
    return list(shares.values()), rejected


def folder_name(title: str, code: str) -> str:
    """暫存子資料夾的名稱：分享標題去掉 115 不收的字元和控制字元、頭尾的空白和點，最多 NAME_MAX 個字；什麼都不剩就用分享碼。"""
    name = EDGE_RE.sub("", BAD_NAME_RE.sub("", title or ""))
    return EDGE_RE.sub("", name[:NAME_MAX]) or code


def _snap_page(body: dict) -> Tuple[dict, int, List[dict], int]:
    """share/snap 的一頁（欄位照 p115client、OpenList，沒實測）：(shareinfo, 最上層共幾項, 這一頁的項目, 這一頁原本幾項)。
    有 fid 的是檔案、沒有的是資料夾（id 是 cid）；id 用字串（19 位）。缺欄位、型別不對都當成沒有，不丟 KeyError。"""
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    info = data.get("shareinfo") if isinstance(data.get("shareinfo"), dict) else {}
    raw = data.get("list") if isinstance(data.get("list"), list) else []
    items = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        is_dir = not it.get("fid")
        item_id = str((it.get("cid") if is_dir else it.get("fid")) or "").strip()
        if not item_id.isdecimal():
            continue
        items.append({"id": item_id, "name": str(it.get("n") or it.get("file_name") or item_id), "is_dir": is_dir,
                      "size": _int(it.get("s"))})
    return info, _int(data.get("count")), items, len(raw)


def _clean_shares(shares) -> List[dict]:
    """網頁送來要轉存的：分享碼、訪問碼只收英數字，ids 只收數字（115 的 id 有 19 位，用字串送、不轉成數字）。
    沒勾任何項目的略過，同一個分享只留一個。"""
    out: Dict[str, dict] = {}
    for s in shares if isinstance(shares, list) else []:
        if not isinstance(s, dict):
            raise ShareError("格式錯誤：shares 要是 [{code, receive_code, title, ids}]")
        code, receive = str(s.get("code") or "").strip(), str(s.get("receive_code") or "").strip()
        if not CODE_RE.fullmatch(code) or receive and not CODE_RE.fullmatch(receive):
            raise ShareError(f"看不懂的分享碼或訪問碼：{code[:64]}")
        ids = [str(i).strip() for i in (s.get("ids") if isinstance(s.get("ids"), list) else [])]
        if not all(i.isdecimal() for i in ids):
            raise ShareError(f"分享 {code} 的項目 id 要是數字")
        if ids and code not in out:
            out[code] = {"code": code, "receive_code": receive, "title": str(s.get("title") or "").strip()[:200] or code,
                         "ids": list(dict.fromkeys(ids))}
    return list(out.values())


def _abort(job: "ShareJob") -> None:
    """背景執行緒出了沒預料到的錯：還停在中間的那幾個寫清楚，不留在「轉存中」「等待中」。"""
    for e in job.shares:
        if e["state"] == WAITING:
            e.update(state=SKIPPED, message="出錯停下，這個沒有轉存")
        elif e["state"] in ("receiving", "waiting_115"):
            e.update(state=FAILED, message="出錯停下" + (f"；檔案可能已經在 {e['path']}，到「瀏覽 115」看" if e["path"] else ""))
        elif e["state"] == "organizing":
            e.update(state=PINNED, message=f"{PINNED_NOTE}；交給 MoviePilot 整理時出錯，到「整理 115 網盤」按「全部整理」")


@dataclass
class ShareJob:
    """轉存分享：一個分享一個分享做，在伺服器上做（關掉網頁也會做完），可以停（做完手上這一個）。

    shares 的每一項：code、title、count（勾了幾項）、state、message、path（暫存子資料夾）、unit_id（整理 115 網盤裡的 id）。
    state：waiting 還沒輪到、receiving 建資料夾和轉存、waiting_115 等 115 轉存完、pinned 已加進整理、organizing 正在交給
    MoviePilot、done 已交給 MoviePilot 整理、failed 失敗、skipped 沒做（按了停止、程式要結束、115 限流）。
    開始時每一項的鍵就都建好，之後只改值：網頁讀狀態（asdict 複製）時背景執行緒正在改，加新鍵會出錯。"""

    running: bool = False
    stopping: bool = False
    stopped: bool = False
    started: float = 0.0
    finished: float = 0.0
    error: str = ""
    current: str = ""
    folder: str = ""
    in_sync: bool = False  # 存到的資料夾在同步目錄裡（整理之前增量同步會先替原始檔名產生 strm）
    target: str = ""  # path：整理到 target_path；空的：先不整理
    target_path: str = ""
    shares: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class ShareTransfer:
    def __init__(self, organizer, strm_sync):
        self.organizer = organizer
        self.strm_sync = strm_sync
        self.workers = Workers()  # 程式結束時停在兩個分享之間（等 115 的時候也會醒來）
        self.job = ShareJob()
        self._starting = threading.Lock()  # 同時只跑一批
        # 等待的間隔做成屬性，測試裡可以調小
        self.pace, self.poll, self.settle, self.wait_max = PACE, POLL, SETTLE, WAIT_MAX

    @property
    def p115(self):
        return self.strm_sync.p115  # 和「整理 115 網盤」用同一個：轉存、釘上整理都對同一個帳號

    def stop(self) -> None:
        """程式要結束：等的地方馬上醒來，手上這一個做到哪算到哪。"""
        self.workers.stop.set()

    def cancel(self) -> bool:
        """按了停止：做完手上這一個分享就停，沒做的不做，也不交給 MoviePilot 整理。沒在轉存回傳 False。"""
        if not self.job.running:
            return False
        self.job.stopping = True
        return True

    def status(self) -> dict:
        return self.job.as_dict()

    def _need_cookie(self) -> None:
        if not self.p115.cookies:
            raise ShareError("轉存分享要用掃碼登入（cookie）")

    def _pause(self) -> bool:
        """兩個請求之間隔開一點（限流剛恢復時放慢）；程式要結束回傳 True。"""
        return self.workers.stop.wait(self.pace * self.p115.breaker.slowdown())

    # ---------------- 讀分享 ----------------

    def read(self, text: str) -> dict:
        """讀出每個分享的標題、大小、最上層的項目（最多 SNAP_MAX 項）：{shares: [{code, receive_code, title, size, count,
        items: [{id, name, is_dir, size}], more, error, warning}], rejected}。讀不到的那個分享在 error 寫 115 的原文；
        115 限流或登入失效就整批停下（再讀下去只會封更久）。"""
        shares, rejected = parse_links(text)
        if not shares:
            raise ShareError("沒有看得懂的分享連結：要是 115.com/s/… 這種網址，一行一個（訪問碼寫在網址的 password= 或同一行）")
        self._need_cookie()
        out = []
        for i, s in enumerate(shares):
            if i and self._pause():
                raise ShareError("程式要結束，沒讀完")
            try:
                out.append(self._snap(s["code"], s["receive_code"]))
            except P115Throttled as exc:
                raise ShareError(f"讀不了分享：{exc}")
            except P115Error as exc:
                out.append({**s, "title": s["code"], "size": 0, "count": 0, "items": [], "more": 0, "error": str(exc),
                            "warning": ""})
        log.info("讀了 %s 個 115 分享（%s 個讀不到）", len(out), sum(1 for s in out if s["error"]))
        return {"shares": out, "rejected": rejected}

    def _snap(self, code: str, receive_code: str) -> dict:
        share = {"code": code, "receive_code": receive_code, "title": "", "size": 0, "count": 0, "items": [], "more": 0,
                 "error": "", "warning": ""}
        info: dict = {}
        offset = 0
        while len(share["items"]) < SNAP_MAX:
            if offset and self._pause():
                break
            self.p115.breaker.check()
            body = self.p115._webapi_get("/share/snap", {"share_code": code, "receive_code": receive_code, "cid": 0,
                                                         "offset": offset, "limit": SNAP_PAGE})
            page_info, count, items, raw = _snap_page(body)
            info = info or page_info
            share["count"] = max(share["count"], count)
            share["items"] += items[:SNAP_MAX - len(share["items"])]
            offset += raw
            if not raw or offset >= count:  # 沒給 count 時也只讀這一頁，不會一直翻
                break
        items = share["items"]
        share["count"] = max(share["count"], len(items))
        share["more"] = share["count"] - len(items)
        share["title"] = str(info.get("share_title") or "").strip() or (items[0]["name"] if len(items) == 1 else code)
        share["size"] = _int(info.get("file_size")) or sum(i["size"] for i in items)
        state = str(info.get("share_state") if info.get("share_state") is not None else "").strip()
        if state not in ("", "1"):  # 1 是正常；其他的照 115 給的原因提醒，不擋（沒實測，猜錯的話還是讓人轉存看看）
            reason = str(info.get("forbid_reason") or "").strip()
            share["warning"] = f"115 說這個分享的狀態是 {state}{'：' + reason if reason else ''}，可能轉存不了"
        if not items:
            share["error"] = "分享裡沒有東西（或 115 沒有列出來）"
        return share

    # ---------------- 轉存 ----------------

    def start(self, shares, folder: str, target: str, target_path: str) -> dict:
        """在背景轉存：shares 是 [{code, receive_code, title, ids}]（ids 是勾的最上層項目）；folder 是存到的 115 資料夾
        （要已經存在，不自動建立）；target 是 path（整理到 target_path）或空的（先不整理）。回傳 job。"""
        self._need_cookie()
        todo = _clean_shares(shares)
        if not todo:
            raise ShareError("沒有勾要轉存的")
        folder = _dir_path(folder)
        if folder == "/":
            raise ShareError("請選要存到哪個 115 資料夾（不能是最上層）")
        if target not in ("path", ""):
            raise ShareError("「整理到」要是一個 115 資料夾，或先不整理")
        target_path = _dir_path(target_path) if target == "path" else ""
        if target_path == "/":
            raise ShareError("請填要整理到哪個 115 資料夾")
        if target and not self.organizer.mp.enabled:
            raise ShareError("還沒設定 MoviePilot，「整理到」只能選先不整理")
        if self.job.running:  # 先擋一次，免得白問 115
            raise ShareError("已經在轉存了，等它做完")
        if self.p115.breaker.tripped:
            raise ShareError(self.p115.breaker.message())
        try:
            cid = self.p115.dir_id(folder)
        except P115Throttled as exc:
            raise ShareError(str(exc))
        except P115Error as exc:
            raise ShareError(f"找不到 115 資料夾 {folder}：{exc}（請先在 115 建好）")
        if not cid:
            raise ShareError(f"找不到 115 資料夾 {folder}")
        with self._starting:
            if self.job.running:
                raise ShareError("已經在轉存了，等它做完")
            self.job = ShareJob(
                running=True, started=time.time(), folder=folder, in_sync=self._in_sync(folder), target=target,
                target_path=target_path,
                shares=[{"code": s["code"], "title": s["title"], "count": len(s["ids"]), "state": WAITING, "message": "",
                         "path": "", "unit_id": ""} for s in todo])
        self.workers.start(self._run, cid, folder, todo)
        log.info("轉存 115 分享：%s 個存到 %s，%s", len(todo), folder, f"整理到 {target_path}" if target else "先不整理")
        return self.job.as_dict()

    def _in_sync(self, folder: str) -> bool:
        return any(_inside(folder, remote_root(t)) for t in self.strm_sync.tasks)

    def _run(self, folder_cid: int, folder: str, todo: List[dict]) -> None:
        job = self.job
        try:
            why = ""
            for i, (entry, share) in enumerate(zip(job.shares, todo)):
                if i:
                    self._pause()  # 程式要結束的話，下面的 _halt 會看到
                why = self._halt(job)
                if why:
                    break
                job.current = entry["title"]
                self._one(entry, share, folder_cid, folder)
            for entry in job.shares:
                if entry["state"] == WAITING:
                    entry.update(state=SKIPPED, message=why)
            job.stopped = job.stopped or job.stopping  # 最後一個做到一半才按停止：沒有沒做的，也要寫「按了停止」
            job.current = ""
            self._organize(job)
            log.info("轉存 115 分享：%s 個加進整理，%s 個失敗，%s 個沒做", sum(1 for e in job.shares if e["unit_id"]),
                     sum(1 for e in job.shares if e["state"] == FAILED), sum(1 for e in job.shares if e["state"] == SKIPPED))
        except Exception as exc:  # 背景執行緒：記下來，不讓網頁一直顯示「轉存中」
            job.error = job.error or f"{type(exc).__name__}: {exc}"
            log.exception("轉存 115 分享時發生錯誤")
            _abort(job)
        finally:
            job.current = ""
            job.running = False
            job.finished = time.time()

    def _halt(self, job: ShareJob) -> str:
        """兩個分享之間：要停下的原因（寫在沒做的那幾個）；繼續做是空字串。"""
        if self.workers.stop.is_set():
            return "程式要結束，這個沒有轉存"
        if job.stopping:
            job.stopped = True
            return "按了停止，這個沒有轉存"
        if self.p115.breaker.tripped:  # 不然每一個都要等到逾時，或報看不懂的錯
            job.error = self.p115.breaker.message()
            return f"{job.error}；這個沒有轉存"
        return ""

    def _one(self, entry: dict, share: dict, folder_cid: int, folder: str) -> None:
        """一個分享：建暫存子資料夾、轉存、等 115 做完、加進「整理 115 網盤」。結果寫在 entry。"""
        entry["state"] = "receiving"
        try:
            cid, path = self._make_folder(folder_cid, folder, folder_name(share["title"], share["code"]), share["code"])
            entry["path"] = path
            self._receive(share, cid, path)
            entry["state"] = "waiting_115"
            self._wait_done(cid, len(share["ids"]), path)
            entry["unit_id"] = self._pin(cid, path)
        except ShareError as exc:
            entry.update(state=FAILED, message=str(exc))
            log.warning("轉存 115 分享 %s 失敗：%s", share["code"], exc)
            return
        entry.update(state=PINNED, message=PINNED_NOTE)

    def _make_folder(self, parent: int, folder: str, name: str, code: str) -> Tuple[int, str]:
        """在存到的資料夾底下建暫存子資料夾；建不了（多半是同名的已經有了，115 回 state 假）就改叫「名稱 (分享碼)」再建一次。"""
        names = [name] + ([f"{name} ({code})"] if name != code else [])
        errors = []
        for n in names:
            path = posixpath.join(folder, n)
            try:
                return self.p115.make_dir(parent, n, path), path
            except P115Throttled as exc:
                raise ShareError(f"建暫存資料夾時，{exc}")
            except P115Error as exc:
                errors.append(f"「{n}」{exc}")
        raise ShareError("建不了暫存資料夾：" + "；".join(errors))

    def _receive(self, share: dict, cid: int, path: str) -> None:
        """POST /share/receive（沒實測）：勾的項目轉存到暫存子資料夾。115 回錯誤就照原文顯示。"""
        try:
            self.p115.breaker.check()
            self.p115._webapi_post("/share/receive", {
                "share_code": share["code"], "receive_code": share["receive_code"], "file_id": ",".join(share["ids"]),
                "cid": str(cid)})
        except P115Error as exc:
            raise ShareError(f"轉存失敗：{exc}（暫存資料夾 {path} 留著，空的話可以到「瀏覽 115」刪掉）")

    def _wait_done(self, cid: int, want: int, path: str) -> None:
        """等 115 轉存完：勾的最上層項目都出現在暫存資料夾裡（新建的資料夾本來是空的，看數量就好，不比對名稱），
        再隔 settle 秒讀兩次資料夾裡（含子資料夾）的檔案數，一樣才算完。最多等 wait_max 秒；按了停止也等完（做完手上這一個）。"""
        deadline = time.monotonic() + self.wait_max
        late = ShareError(f"115 還在轉存，等它做完到「瀏覽 115」把 {path} 加進整理")
        try:
            while len(self._list(cid)) < want:
                if time.monotonic() >= deadline:
                    raise late
                self._sleep(self.poll, path)
            last = self._count(cid)
            while True:
                self._sleep(self.settle, path)
                now = self._count(cid)
                if now is None or last is None or now == last:  # 讀不到檔案數（沒實測的介面）：多等了一次就算完
                    return
                if time.monotonic() >= deadline:
                    raise late
                last = now
        except P115Error as exc:
            raise ShareError(f"等 115 轉存時讀不到暫存資料夾：{exc}；檔案可能已經在 {path}，到「瀏覽 115」看")

    def _sleep(self, seconds: float, path: str) -> None:
        """等 115 時的間隔（限流剛恢復時放慢）。只有程式要結束才提早醒來；按了停止照樣等完這一個。"""
        if self.workers.stop.wait(seconds * self.p115.breaker.slowdown()):
            raise ShareError(f"程式要結束，沒等到 115 轉存完；檔案會在 {path}，到「瀏覽 115」把它加進整理")

    def _list(self, cid: int) -> List[dict]:
        self.p115.breaker.check()
        return self.p115.list_dir(cid)

    def _count(self, cid: int) -> Optional[int]:
        """資料夾裡（含子資料夾）有幾個檔案：GET /category/get 的 count（是字串）；讀不到、看不懂回傳 None。"""
        self.p115.breaker.check()
        try:
            body = self.p115._webapi_get("/category/get", {"cid": cid, "aid": 1})
        except P115Throttled:
            raise
        except P115Error as exc:
            log.info("讀不到 115 資料夾 %s 的檔案數：%s", cid, exc)
            return None
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        count = body.get("count", data.get("count"))
        return _int(count) if str(count if count is not None else "").strip().isdecimal() else None

    def _pin(self, cid: int, path: str) -> str:
        """加進「整理 115 網盤」（和瀏覽 115 的「整理這個資料夾…」一樣：列一次目錄、問 MoviePilot 叫什麼，釘在最上面）。"""
        try:
            return str(self.organizer.folder_unit(cid, path)["id"])
        except OrganizeError as exc:
            raise ShareError(f"已轉存到 {path}，但加不進「整理 115 網盤」：{exc}")

    def _organize(self, job: ShareJob) -> None:
        """都做完之後：選了「整理到」的，加進整理的那幾個交給「整理 115 網盤」在背景整理（整理完搬空的暫存子資料夾移到
        115 回收站）。釘選清單滿了被擠掉的寫明；按了停止、程式要結束、115 限流時不交出去。"""
        pinned = [e for e in job.shares if e["state"] == PINNED]
        for e in pinned:
            if not self.organizer.is_pinned(e["unit_id"]):
                e.update(unit_id="", message=f"已轉存到 {e['path']}；「整理 115 網盤」的釘選清單滿了（最多 {MAX_PINNED} 個），"
                                             "請到「瀏覽 115」把它加進整理")
        live = [e for e in pinned if e["unit_id"]]
        if job.target != "path" or not live:
            return
        why = ("程式要結束" if self.workers.stop.is_set() else "按了停止" if job.stopping
               else self.p115.breaker.message() if self.p115.breaker.tripped else "")
        if why:
            job.stopped = job.stopped or job.stopping
            for e in live:
                e["message"] = f"{PINNED_NOTE}；{why}，沒有交給 MoviePilot 整理"
            return
        for e in live:
            e["state"] = "organizing"
        batch = self.organizer.organize_units_in_background([e["unit_id"] for e in live], "path", job.target_path,
                                                            cleanup=True)
        for e in live:
            if batch is None:
                e.update(state=PINNED, message=f"{PINNED_NOTE}；目前有別的整理在跑，等它做完再按「全部整理」")
            else:
                e.update(state="done", message=f"已交給 MoviePilot 整理到 {job.target_path}：到「整理 115 網盤」看結果")
