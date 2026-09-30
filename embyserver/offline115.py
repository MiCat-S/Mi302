"""115 雲下載（離線下載）：把磁力、ed2k、http、ftp 連結交給 115，下載到網盤裡。

授權了 115 開放平台就用它（proapi.115.com/open/offline/*，有文件）；沒有就用 cookie，照開源的 p115client（MIT）的做法：
- 加任務：clouddownload.115.com/lixianssp/，內容用 115 的 RSA 加密，User-Agent 要是安卓 115；
- 任務清單、刪除、清除、配額：clouddownload.115.com/web/，一般的 cookie 請求。
cookie 這條沒有官方文件，115 改了介面就會失敗，錯誤訊息照實顯示。

下載好的檔案在 115 上：存到同步目錄裡的，下一次增量同步就會產生 strm；不在同步目錄裡的，可以到「瀏覽 115」整理。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional

import httpx

from .p115 import P115Error, P115Service, rsa_decrypt
from .p115_open import P115OpenError

log = logging.getLogger(__name__)

CLOUD = "https://clouddownload.115.com"
APP_VER = "36.2.28"  # 安卓 115 的版本號；加任務的 lixianssp 介面要帶
ANDROID_UA = f"Mozilla/5.0 115disk/{APP_VER} 115Browser/{APP_VER} 115wangpan_android/{APP_VER}"
MAX_URLS = 200  # 一次最多送幾個連結（再多分幾次送）
URL_PREFIXES = ("magnet:?", "ed2k://", "http://", "https://", "ftp://")
INFO_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$|^[2-7A-Za-z]{32}$")  # 只貼了種子的 info hash（十六進位或 base32）
# 清除任務：115 的 flag。只開放「已完成」「已失敗」，不開放會連檔案一起刪的 4、5，也不開放會取消下載中的 1、3
CLEAR_FLAGS = {"done": 0, "failed": 2}


class OfflineError(Exception):
    pass


def clean_urls(text: str) -> tuple:
    """貼上的內容：一行一個（也接受空白分隔的磁力）。回傳 (可以送的, 看不懂的)，重複的只留一個。"""
    ok: List[str] = []
    bad: List[str] = []
    for line in re.split(r"[\r\n]+", text or ""):
        line = line.strip()
        if not line:
            continue
        if INFO_HASH_RE.match(line):
            line = f"magnet:?xt=urn:btih:{line}"
        if line.lower().startswith(URL_PREFIXES):
            if line not in ok:
                ok.append(line)
        else:
            bad.append(line)
    return ok, bad


def _task(t: dict) -> dict:
    """開放平台和 cookie 的任務欄位大致一樣：info_hash、name、size、percentDone、status（2 完成、1 下載中、
    -1 失敗、0 等待）、file_id（下載好的檔案或資料夾）、wp_path_id（存到的資料夾）。"""
    try:
        pct = float(t.get("percentDone") if t.get("percentDone") is not None else t.get("percent_done") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    try:
        status = int(t.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    state = "done" if status == 2 or pct >= 100 else "failed" if status < 0 else "downloading" if status == 1 else "waiting"
    return {
        "hash": str(t.get("info_hash") or t.get("hash") or ""), "name": str(t.get("name") or t.get("url") or ""),
        "size": int(float(t.get("size") or 0)), "percent": round(min(max(pct, 0.0), 100.0), 1), "state": state,
        "file_id": str(t.get("file_id") or ""), "folder_id": str(t.get("wp_path_id") or ""),
        "added": int(float(t.get("add_time") or 0)), "updated": int(float(t.get("last_update") or 0)),
        "url": str(t.get("url") or ""),
    }


def _results(body: dict, urls: List[str]) -> List[dict]:
    """加任務的回應：每個連結的結果在 data 或 result 清單裡（沒有的話整批照 state 算）。"""
    items = body.get("data") if isinstance(body.get("data"), list) else body.get("result")
    if not isinstance(items, list):
        ok = bool(body.get("state", True))
        return [{"url": u, "ok": ok, "message": "" if ok else str(body.get("error_msg") or body.get("message") or "")}
                for u in urls]
    out = []
    for i, item in enumerate(items):
        item = item if isinstance(item, dict) else {}
        code = item.get("errcode", item.get("code", 0))
        ok = bool(item.get("state", True)) and not code
        out.append({"url": str(item.get("url") or (urls[i] if i < len(urls) else "")), "ok": ok, "name": str(item.get("name") or ""),
                    "message": "" if ok else str(item.get("error_msg") or item.get("message") or item.get("errmsg") or code)})
    return out


class OfflineDownloads:
    def __init__(self, p115: P115Service):
        self.p115 = p115

    @property
    def via(self) -> str:
        return "open" if self.p115.open.authorized else "cookie"

    # ---------------- 加任務 ----------------

    def add(self, text: str, folder: str = "", folder_id: int = 0) -> dict:
        """把連結交給 115 下載。folder 是存到的 115 資料夾路徑，空的是 115 預設的「雲下載」資料夾；
        給了 folder_id（重新加入失敗的任務時用原來的資料夾）就不查路徑。"""
        urls, bad = clean_urls(text)
        if not urls:
            raise OfflineError("沒有可以下載的連結：要是磁力（magnet:?）、ed2k://、http(s)://、ftp://，一行一個")
        folder = "/" + str(folder or "").strip().strip("/") if str(folder or "").strip("/ ") else ""
        try:
            cid = int(folder_id) if folder_id else self.p115.dir_id(folder) if folder else 0
        except P115Error as exc:
            raise OfflineError(f"找不到 115 資料夾 {folder}：{exc}")
        results: List[dict] = []
        for start in range(0, len(urls), MAX_URLS):
            chunk = urls[start:start + MAX_URLS]
            body = self._call("加雲下載任務", lambda: self._open_add(chunk, cid), lambda: self._cookie_add(chunk, cid))
            results += _results(body, chunk)
        added = sum(1 for r in results if r["ok"])
        log.info("115 雲下載：加了 %s 個任務（%s 個失敗）到 %s", added, len(results) - added, folder or "預設資料夾")
        return {"added": added, "results": results, "rejected": bad, "folder": folder, "via": self.via}

    def retry(self, info_hash: str, url: str, folder_id: int = 0) -> dict:
        """重新加入失敗的任務：先刪掉那一筆失敗的紀錄（不動 115 上的檔案，不然 115 會說「任務已存在」），
        再用原來的連結加到原來的資料夾。"""
        if not clean_urls(url)[0]:
            raise OfflineError("這個任務沒有原來的連結，沒辦法重新加入")
        self.delete([info_hash], with_files=False)
        result = self.add(url, "", folder_id)
        log.info("115 雲下載：重新加入 %s", info_hash)
        return result

    def _open_add(self, urls: List[str], cid: int) -> dict:
        form: Dict[str, str] = {"urls": "\n".join(urls)}
        if cid:
            form["wp_path_id"] = str(cid)
        return self.p115.open._call("POST", "/open/offline/add_task_urls", form=form)

    def _cookie_add(self, urls: List[str], cid: int) -> dict:
        from p115cipher import rsa_encrypt

        payload = {f"url[{i}]": u for i, u in enumerate(urls)}
        if cid:
            payload["wp_path_id"] = str(cid)
        payload.update(ac="add_task_urls", app_ver=APP_VER)
        data = rsa_encrypt(json.dumps(payload, separators=(",", ":")).encode()).decode("ascii")
        resp = self.p115._client.post(f"{CLOUD}/lixianssp/", data={"data": data},
                                      headers={"Cookie": self.p115.cookies, "User-Agent": ANDROID_UA})
        return self._cookie_body(resp)

    # ---------------- 任務清單、配額 ----------------

    def tasks(self, page: int = 1) -> dict:
        page = max(1, int(page or 1))
        body = self._call("讀雲下載任務", lambda: self.p115.open._call("GET", "/open/offline/get_task_list", params={"page": page}),
                          lambda: self._cookie_get({"ac": "task_lists", "page": page, "page_size": 30}))
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        tasks = [_task(t) for t in data.get("tasks") or [] if isinstance(t, dict)]
        return {"tasks": tasks, "page": int(data.get("page") or page), "pages": max(1, int(data.get("page_count") or 1)),
                "total": int(data.get("count") or len(tasks)), "quota": self.quota(), "via": self.via}

    def quota(self) -> dict:
        """這個月還能加幾個任務；讀不到不影響其他的，回傳空的。"""
        try:
            body = self._call("讀雲下載配額", lambda: self.p115.open._call("GET", "/open/offline/get_quota_info"),
                              lambda: self._cookie_get({"ac": "get_quota_info"}))
        except OfflineError as exc:
            log.info("讀不到 115 雲下載配額：%s", exc)
            return {}
        data = body.get("data") if isinstance(body.get("data"), dict) else body

        def num(*keys) -> Optional[int]:
            for k in keys:
                if str(data.get(k, "")).lstrip("-").isdigit():
                    return int(data[k])
            return None

        return {k: v for k, v in (("left", num("surplus", "quota")), ("total", num("count", "total")), ("used", num("used")))
                if v is not None}

    # ---------------- 刪除、清除 ----------------

    def delete(self, hashes: List[str], with_files: bool = False) -> dict:
        """刪掉這幾個任務；with_files 時連 115 上下載好的檔案一起刪（115 的「刪除源文件」）。"""
        hashes = [h for h in dict.fromkeys(str(h).strip() for h in hashes) if re.fullmatch(r"[0-9A-Za-z]{16,64}", h)]
        if not hashes:
            raise OfflineError("沒有要刪的任務")
        flag = "1" if with_files else "0"

        def by_open() -> dict:
            for h in hashes:  # 開放平台一次刪一個
                self.p115.open._call("POST", "/open/offline/del_task", form={"info_hash": h, "del_source_file": flag})
            return {"state": True}

        def by_cookie() -> dict:
            form = {f"hash[{i}]": h for i, h in enumerate(hashes)}
            form.update(ac="task_del", flag=flag)
            return self._cookie_post(form)

        self._call("刪雲下載任務", by_open, by_cookie)
        log.info("115 雲下載：刪了 %s 個任務%s", len(hashes), "（連同下載好的檔案）" if with_files else "")
        return {"deleted": len(hashes), "with_files": with_files}

    def clear(self, what: str) -> dict:
        """清掉已完成（done）或已失敗（failed）的任務紀錄；不動下載好的檔案。"""
        if what not in CLEAR_FLAGS:
            raise OfflineError("只能清除已完成或已失敗的任務")
        flag = str(CLEAR_FLAGS[what])
        self._call("清除雲下載任務", lambda: self.p115.open._call("POST", "/open/offline/clear_task", form={"flag": flag}),
                   lambda: self._cookie_post({"ac": "task_clear", "flag": flag}))
        return {"cleared": what}

    # ---------------- 共用 ----------------

    def _call(self, action: str, open_call, cookie_call) -> dict:
        try:
            return self.p115._dispatch(action, open_call, cookie_call)
        except (P115Error, P115OpenError, httpx.HTTPError) as exc:
            raise OfflineError(f"{action}失敗：{exc}") from exc

    def _cookie_get(self, params: dict) -> dict:
        resp = self.p115._client.get(f"{CLOUD}/web/", params=params, headers=self.p115._cookie_headers())
        return self._cookie_body(resp)

    def _cookie_post(self, form: dict) -> dict:
        resp = self.p115._client.post(f"{CLOUD}/web/", data=form, headers=self.p115._cookie_headers())
        return self._cookie_body(resp)

    def _cookie_body(self, resp: httpx.Response) -> dict:
        """cookie 通道的回應：可能整個是加密的文字、或 data 欄位是加密的；限流、登入失效照樣熔斷。"""
        try:
            body = self.p115._api_json(resp)
        except P115Error:
            try:  # 整個回應是 115 的 RSA 加密文字
                body = json.loads(rsa_decrypt(resp.text.strip()))
            except Exception:
                raise P115Error(f"115 雲下載回應看不懂：HTTP {resp.status_code}")
        if isinstance(body.get("data"), str) and body["data"]:
            try:
                body["data"] = json.loads(rsa_decrypt(body["data"]))
            except Exception:
                pass
        if not body.get("state", True):
            raise P115Error(str(body.get("error_msg") or body.get("error") or body.get("message") or body.get("errcode") or body))
        return body
