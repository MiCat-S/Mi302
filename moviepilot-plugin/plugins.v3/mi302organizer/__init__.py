"""Mi302 整理助手：讓 Mi302 照 MoviePilot 自己的規則算新名字，再用它的 115 授權直接批次改名。

MoviePilot 的手動整理一個檔案要查來源、查目標、移動、再查、改名，大約 7～8 個開放平台請求；資料夾結構已經對、
只是名字不照格式時，其實每個檔案、資料夾改一次名就好（POST /open/ufile/update，一個請求）。Mi302 照
MoviePilot 預覽算好的新名字，把要改的清單送過來，這裡在背景照順序一個一個改，用的是 MoviePilot 的 115 存儲，
限速（每秒 3 個請求）和 429 冷卻都和它共用。

MoviePilot 的整理預覽一個檔案要一兩秒（認片、抓圖、比對目錄設定都跑一遍），幾百集的資料夾要好幾分鐘、還吃記憶體；
/names 只呼叫它算名字的那幾個函式（見 naming.py），同一部片只認一次，幾百集幾秒就好，名字和它整理的一模一樣。

介面（掛在 /api/v1/plugin/Mi302Organizer 底下，用 MoviePilot 的登入 token 或 API 令牌）：
- GET  /status：有沒有開、是否正在改、會哪些功能（features）；self_test 是這版 MoviePilot 少了的內部函式
  （少了就不列 names，Mi302 改用它的整理預覽）
- POST /names：{"items": [{"path", "fileid"?, "size"?}], "tmdbid"?, "type"?（电视剧／电影）, "season"?,
  "episode_format"?}，照它的規則算每個檔案整理後的名字（相對於媒體庫目錄）；只算，不改
- POST /rename：{"items": [{"fileid", "name", "old"?, "path"?, "type"?}]}，回傳工作 id；照清單的順序改
- GET  /job?id=：進度和每一項的結果
- POST /cancel?id=：做完手上這一項就停
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body

from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

MAX_ITEMS = 10000  # 一次最多改幾項
KEEP_JOBS = 20  # 記住最近幾次的結果


class Mi302Organizer(_PluginBase):
    plugin_name = "Mi302 整理助手"
    plugin_desc = "讓 Mi302 用 MoviePilot 的 115 授權直接批次改名，比整理流程快很多。"
    plugin_icon = "https://raw.githubusercontent.com/MiCat-S/Mi302/main/embyserver/web/icon-192.png"
    plugin_version = "1.2.0"
    plugin_author = "MiCat-S"
    author_url = "https://github.com/MiCat-S/Mi302"
    plugin_order = 99
    auth_level = 1

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._jobs: Dict[str, dict] = {}
        self._lock = threading.Lock()  # 同時只改一批（共用 MoviePilot 的 115 限速，並行也不會比較快）
        self._jobs_lock = threading.Lock()
        self._stop = threading.Event()
        self._missing: Optional[List[str]] = None  # 算名字要用、這版 MoviePilot 卻沒有的內部函式；第一次問 /status 時查

    # ---------------- 外掛的基本介面 ----------------

    def init_plugin(self, config: Optional[dict] = None) -> None:
        self._enabled = bool((config or {}).get("enabled"))
        self._stop.clear()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        # "bear"：MoviePilot 的登入 token 或 API 令牌都可以（Mi302 用帳號密碼登入時帶的是 token）
        return [
            {"path": "/status", "endpoint": self.api_status, "auth": "bear", "methods": ["GET"],
             "summary": "Mi302 整理助手的狀態"},
            {"path": "/names", "endpoint": self.api_names, "auth": "bear", "methods": ["POST"],
             "summary": "照 MoviePilot 的整理規則算新名字（只算，不改）"},
            {"path": "/rename", "endpoint": self.api_rename, "auth": "bear", "methods": ["POST"],
             "summary": "在背景照順序批次改名 115 上的檔案、資料夾"},
            {"path": "/job", "endpoint": self.api_job, "auth": "bear", "methods": ["GET"],
             "summary": "改名工作的進度和結果"},
            {"path": "/cancel", "endpoint": self.api_cancel, "auth": "bear", "methods": ["POST"],
             "summary": "停止改名工作（做完手上這一項）"},
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {"component": "VForm", "content": [
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                        {"component": "VSwitch", "props": {"model": "enabled", "label": "啟用"}},
                    ]},
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12}, "content": [
                        {"component": "VAlert", "props": {
                            "type": "info", "variant": "tonal",
                            "text": "給 Mi302 用：Mi302「整理 115 網盤」預覽時請這裡照 MoviePilot 的規則算新名字，"
                                    "只需要改名的資料夾再交給這裡用 MoviePilot 的 115 授權直接改名，不走整理流程。"
                                    "這裡不會自己去改任何東西。"}},
                    ]},
                ]},
            ]},
        ], {"enabled": False}

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self) -> None:
        self._stop.set()

    # ---------------- HTTP 介面 ----------------

    def api_status(self) -> Dict[str, Any]:
        if self._missing is None:
            from .naming import missing_internals

            self._missing = missing_internals()
            if self._missing:
                logger.warning(f"Mi302 整理助手：這版 MoviePilot 少了 {', '.join(self._missing)}，不算名字（Mi302 改用整理預覽）")
        return {"enabled": self._enabled, "version": self.plugin_version, "busy": self._lock.locked(),
                "features": ["rename"] + ([] if self._missing else ["names"]), "self_test": self._missing}

    def api_names(self, payload: dict = Body(...)) -> Dict[str, Any]:
        if not self._enabled:
            return {"success": False, "message": "Mi302 整理助手沒有啟用"}
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            return {"success": False, "message": "沒有要算名字的檔案"}
        if len(items) > MAX_ITEMS:
            return {"success": False, "message": f"一次最多 {MAX_ITEMS} 個檔案"}
        if any("/BDMV/" in str((it or {}).get("path") or "") for it in items):
            return {"success": False, "message": "藍光原碟要整個資料夾交給 MoviePilot 整理"}
        started = time.time()
        try:
            from .naming import Namer

            results = Namer(payload).run(items)
        except Exception as exc:  # MoviePilot 改版、函式換了：Mi302 會改用它的整理預覽
            logger.error(f"Mi302 整理助手：算名字出錯：{exc}")
            return {"success": False, "message": f"{type(exc).__name__}: {exc}"}
        logger.info(f"Mi302 整理助手：算了 {len(results)} 個檔案的名字，用了 {time.time() - started:.1f} 秒")
        return {"success": True, "items": results}

    def api_rename(self, payload: dict = Body(...)) -> Dict[str, Any]:
        if not self._enabled:
            return {"success": False, "message": "Mi302 整理助手沒有啟用"}
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            return {"success": False, "message": "沒有要改名的項目"}
        if len(items) > MAX_ITEMS:
            return {"success": False, "message": f"一次最多 {MAX_ITEMS} 項"}
        clean = []
        for it in items:
            fid, name = str((it or {}).get("fileid") or "").strip(), str((it or {}).get("name") or "").strip()
            if not fid.isdigit() or not name or "/" in name or name in (".", ".."):
                return {"success": False, "message": f"格式不對：{it}"}
            clean.append({"fileid": fid, "name": name, "old": str(it.get("old") or ""), "path": str(it.get("path") or ""),
                          "type": "dir" if it.get("type") == "dir" else "file"})
        if not self._lock.acquire(blocking=False):
            return {"success": False, "message": "上一批還在改，等它做完"}
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "state": "running", "total": len(clean), "done": 0, "ok": 0, "failed": 0,
               "started": time.time(), "finished": 0.0, "current": "", "cancel": False, "results": []}
        with self._jobs_lock:
            self._jobs[jid] = job
            for old in sorted(self._jobs, key=lambda k: self._jobs[k]["started"])[:-KEEP_JOBS]:
                self._jobs.pop(old, None)
        threading.Thread(target=self._run, args=(job, clean), daemon=True, name="Mi302Organizer.rename").start()
        return {"success": True, "job": jid}

    def api_job(self, id: str = "") -> Dict[str, Any]:  # noqa: A002 - 查詢參數名稱
        job = self._jobs.get(id)
        if not job:
            return {"success": False, "message": "找不到這個工作"}
        return {"success": True, **{k: v for k, v in job.items() if k != "cancel"}}

    def api_cancel(self, id: str = "") -> Dict[str, Any]:  # noqa: A002
        job = self._jobs.get(id)
        if not job or job["state"] != "running":
            return {"success": False, "message": "這個工作沒在跑"}
        job["cancel"] = True
        return {"success": True}

    # ---------------- 改名 ----------------

    def _run(self, job: dict, items: List[dict]) -> None:
        """照清單的順序一項一項改（呼叫的人負責順序：先檔案、再裡面的資料夾、最後外面的資料夾）。"""
        try:
            from app.chain.storage import StorageChain
            from app.schemas.file import FileItem

            chain = StorageChain()
            logger.info(f"Mi302 整理助手：開始改名 {len(items)} 項")
            for it in items:
                if job["cancel"] or self._stop.is_set():
                    job["state"] = "stopped"
                    break
                job["current"] = it["old"] or it["name"]
                item = FileItem(storage="u115", fileid=it["fileid"], type=it["type"], path=it["path"] or "/",
                                name=it["old"] or it["name"])
                try:
                    ok = bool(chain.rename_file(item, it["name"]))
                    message = "" if ok else "115 不接受（原因看 MoviePilot 的日誌）"
                except Exception as exc:  # 一項出錯不影響其他項
                    ok, message = False, f"{type(exc).__name__}: {exc}"
                job["done"] += 1
                job["ok" if ok else "failed"] += 1
                job["results"].append({"fileid": it["fileid"], "old": it["old"], "name": it["name"], "type": it["type"],
                                       "ok": ok, "message": message})
                if not ok:
                    logger.warning(f"Mi302 整理助手：{it['old'] or it['fileid']} 改成 {it['name']} 失敗：{message}")
            else:
                job["state"] = "done"
            logger.info(f"Mi302 整理助手：改名結束，成功 {job['ok']}、失敗 {job['failed']}、共 {job['total']} 項")
        except Exception as exc:  # 背景執行緒：記下來，不讓工作一直停在「進行中」
            job["state"] = "error"
            job["results"].append({"fileid": "", "old": "", "name": "", "type": "", "ok": False,
                                   "message": f"{type(exc).__name__}: {exc}"})
            logger.error(f"Mi302 整理助手：改名出錯：{exc}")
        finally:
            job["current"] = ""
            job["finished"] = time.time()
            self._lock.release()
