"""整理的執行和刪除（「整理 115 網盤」預覽過的交給這裡執行），以及集數定位模板的小工具。

- 執行（execute_in_background）：照一個或幾個預覽代碼送 MoviePilot 整理（每一批是一個資料夾或幾個檔案），
  結果照它回的每個檔案記；完成後刪掉本機寫著 -1 的舊 nfo（免得同步時它跟著 strm 搬到新名字），
  沒有影片留下的來源資料夾移到 115 回收站（cleanup，先確認資料夾 id 還在原本的路徑），再跑增量同步。
- 刪除：劇的任何一集（delete_episodes）、整部劇（delete_series）、一個資料夾或檔案（delete_item），
  都是送進 115 回收站（可以還原），本機 strm、nfo 和媒體庫跟著拿掉。
- 集數定位（episode_template）：MoviePilot 推薦不出來時的備用，依 Mi302 在檔名裡找到集號的位置產生模板
  （「10.潘玮柏…」是 {ep}.{a}、「03-比赛…」是 {ep}-{a}）。
"""

from __future__ import annotations

import logging
import posixpath
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .config import StrmTask
from .db import Database
from .dupes import DELETE_BATCH
from .filetypes import VIDEO_EXTS
from .moviepilot import MoviePilot, MoviePilotError
from .p115 import P115Error
from .scanner import episode_match, parse_nfo
from .strm_sync import INCREMENTAL, remote_root, task_key

log = logging.getLogger(__name__)

# EPISODE_PATTERNS 裡要告訴 MoviePilot 集號在哪的寫法：第N集、開頭就是集號。SxxEyy、1x02、EP02 它自己認得
TEMPLATE_PATTERNS = {2, 4, 5}
PREVIEW_TTL = 1800  # 預覽多久內可以執行
EP_TEXT_RE = re.compile(r"^([Ee][Pp]?)?(\d{1,4})(-([Ee][Pp]?)?(\d{1,4}))?$")  # MoviePilot 對 {ep} 內容的要求
NEGATIVE_NUMBER_RE = re.compile(r"<(season|episode)>\s*-\d+\s*</\1>")  # 刮削時沒認出集號寫的 -1


class ReorgError(Exception):
    pass


def _esc(text: str) -> str:
    return text.replace("{", "{{").replace("}", "}}")


def episode_template(name: str) -> Optional[str]:
    """依 Mi302 在檔名裡找到集號的位置，產生 MoviePilot 的集數定位模板；不需要模板時回傳 None。

    模板裡 {ep} 是集號，{a}、{b} 是任意文字。例：「10.潘玮柏战队.mp4」→ {ep}.{a}，「某剧 第10集.mp4」→ {b}第{ep}集{a}。
    """
    stem = posixpath.splitext(name)[0]
    found = episode_match(stem)
    if not found or found[1] not in TEMPLATE_PATTERNS:
        return None
    m = found[0]
    start, end = m.span("episode")
    if not stem[start:end].isdigit():
        return None  # 中文數字（第十二集）：模板取不出來，交給 MoviePilot 自己認
    prefix = ("{b}" if m.start() > 0 else "") + _esc(stem[m.start():start])
    suffix = _esc(name[end]) + "{a}" if end < len(name) else ""
    return prefix + "{ep}" + suffix


def _compile(template: str) -> Optional["re.Pattern"]:
    """照 MoviePilot 的規則把模板變成正則：{{ }} 是大括號本身，{ep} 至少一個字、其他佔位符可以是空的，要整個檔名對上。"""
    parts, i = ["^"], 0
    while i < len(template):
        if template.startswith("{{", i) or template.startswith("}}", i):
            parts.append(re.escape(template[i]))
            i += 2
        elif template[i] == "{":
            end = template.find("}", i + 1)
            name = template[i + 1:end] if end > 0 else ""
            if not re.fullmatch(r"[A-Za-z_]\w*", name):
                return None
            parts.append(f"(?P<{name}>{'.+?' if name == 'ep' else '.*?'})")
            i = end + 1
        elif template[i] == "}":
            return None
        else:
            j = i
            while j < len(template) and template[j] not in "{}":
                j += 1
            parts.append(re.escape(template[i:j]))
            i = j
    parts.append("$")
    try:
        return re.compile("".join(parts))
    except re.error:  # 同一個佔位符用了兩次
        return None


def template_episode(template: str, name: str) -> Optional[int]:
    """用這個模板從檔名取出的集號；對不上或取出來的不是集號時回傳 None。"""
    pat = _compile(template)
    m = pat.match(name) if pat else None
    if not m or "ep" not in m.groupdict():
        return None
    ep = EP_TEXT_RE.match(m.group("ep"))
    return int(ep.group(2)) if ep else None


@dataclass
class ReorgJob:
    running: bool = False
    started: float = 0.0
    finished: float = 0.0
    title: str = ""
    total: int = 0
    done: int = 0  # MoviePilot 整理好（或已接收）的檔案
    failed: int = 0
    current: str = ""
    synced: str = ""  # 之後的增量同步：started / busy
    items: List[dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class Reorganizer:
    def __init__(self, db: Database, strm_sync, moviepilot: MoviePilot, scanner=None):
        self.db = db
        self.strm_sync = strm_sync
        self.mp = moviepilot
        self.scanner = scanner  # 刪除之後把它們從媒體庫拿掉
        self.job = ReorgJob()
        self.sync_delay = 20.0  # 等 115 記下這次的移動、改名，再跑增量同步
        self._lock = threading.Lock()  # 整理和刪除不同時做
        self._previews: dict = {}

    @property
    def p115(self):
        return self.strm_sync.p115

    def ready(self) -> dict:
        """缺什麼就不能整理：MoviePilot 的帳號密碼（手動整理只接受帳號登入）、115 登入。"""
        return {"moviepilot": self.mp.enabled, "login": self.mp.can_subscribe, "p115": self.p115.logged_in}

    def _locate(self, local: str) -> Tuple[Optional[StrmTask], str]:
        """本機路徑在哪個同步任務的資料夾裡，以及相對於那個資料夾的路徑；不在任何任務裡回傳 (None, "")。"""
        for task in self.strm_sync.tasks:
            try:
                return task, Path(local).relative_to(Path(task.local).expanduser()).as_posix()
            except ValueError:
                continue
        return None, ""

    # ---------------- 刪除 ----------------

    def _series_folder(self, series_id: int) -> Tuple[dict, object, str, Optional[int], str]:
        """劇、它所在的同步任務、相對路徑、115 上的資料夾 id 和完整路徑（劇集資料夾就是同步目錄本身時 id 是 None）。"""
        series = self.db.one("SELECT id, name, year, path FROM items WHERE id=? AND type='Series'", (series_id,))
        if not series:
            raise ReorgError("找不到這部劇")
        task, rel = self._locate(series["path"])
        if not task:
            raise ReorgError("這部劇不在任何 115 同步任務的本機資料夾裡，刪不到 115 上的檔案")
        row = self.db.one("SELECT file_id FROM p115_index WHERE task=? AND path=? AND is_dir=1", (task_key(task), rel)) \
            if rel != "." else None
        return dict(series), task, rel, row["file_id"] if row else None, \
            posixpath.join(remote_root(task), rel) if rel != "." else remote_root(task)

    def series_files(self, series_id: int, season: Optional[int] = None) -> dict:
        """整部劇在媒體庫裡的每一集（照季、集排），給網頁勾要刪哪些。problem 是集號是猜的、或認不出的集
        （給了 season 就只算那一季）。
        附上劇集資料夾：115 上的路徑、同步紀錄裡有幾支影片（含沒進媒體庫的）。只看資料庫，不向 115 請求。"""
        series, task, rel, cid, path = self._series_folder(series_id)
        key, local = task_key(task), str(Path(task.local).expanduser())
        prefix = "" if rel == "." else rel + "/"
        ids = {r["path"]: r["file_id"] for r in self.db.query(
            "SELECT file_id, path FROM p115_index WHERE task=? AND is_dir=0 AND path>=? AND path<?",
            (key, prefix, prefix + "\U0010ffff"))}
        files = []
        for r in self.db.query(
                "SELECT path, parent_index_number AS season, index_number, ep_from FROM items WHERE series_id=? "
                "AND type='Episode' AND is_strm=1 ORDER BY parent_index_number, index_number, path", (series_id,)):
            file_rel = Path(r["path"]).relative_to(local).as_posix() if r["path"].startswith(local + "/") else ""
            problem = r["ep_from"] in ("name", "none") and (season is None or r["season"] == season)
            files.append({"file_id": ids.get(file_rel), "name": posixpath.splitext(posixpath.basename(file_rel))[0],
                          "folder": posixpath.dirname(file_rel), "season": r["season"],
                          "episode": r["index_number"] if r["ep_from"] != "none" else None, "ep_from": r["ep_from"],
                          "problem": problem})
        videos = sum(1 for p in ids if p.endswith(".strm"))
        return {"series_id": series_id, "season": season, "name": series["name"], "year": series["year"], "files": files,
                "folder": {"cid": cid, "path": path, "videos": videos}}

    def _begin_delete(self) -> None:
        if self.strm_sync.result.running:
            raise ReorgError("115 正在同步，等同步完成再刪")
        if not self._lock.acquire(blocking=False):
            raise ReorgError("MoviePilot 正在整理，等它完成再刪")

    def delete_episodes(self, series_id: int, file_ids: List[int], remove_folder: bool) -> dict:
        """把這部劇勾選的集送進 115 回收站，本機 strm、nfo 和媒體庫跟著拿掉。只能刪這部劇的集（任何一季都可以）。

        remove_folder 時，劇集資料夾刪完沒有影片（115 上實際去看）就一起移到回收站，裡面剩下的 nfo、圖片一起走；
        資料夾 id 對不上原本的路徑時不動它。
        """
        plan = self.series_files(series_id)
        allowed = {f["file_id"] for f in plan["files"] if f["file_id"]}
        ids = list(dict.fromkeys(int(i) for i in file_ids))
        if not ids:
            raise ReorgError("沒有勾要刪的集")
        if any(i not in allowed for i in ids):
            raise ReorgError("勾的檔案不在這部劇裡（清單可能已經過期），請重新打開")
        self._begin_delete()
        deleted: List[int] = []
        folder, folder_removed, note = plan["folder"], False, ""
        try:
            for start in range(0, len(ids), DELETE_BATCH):
                batch = ids[start:start + DELETE_BATCH]
                self.p115.delete_files(batch)
                deleted += batch
            if remove_folder and folder["cid"]:
                if self._folder_path(folder["cid"]) != folder["path"]:
                    note = "劇集資料夾已經不在原本的位置，沒有動它"
                else:
                    left = self._videos_left(folder["cid"])
                    if left:
                        note = f"劇集資料夾還有 {left} 支影片，資料夾保留"
                    else:
                        self.p115.delete_files([folder["cid"]])
                        folder_removed = True
        except P115Error as exc:
            raise ReorgError(f"115 刪除失敗：{exc}" + (f"（已經刪了 {len(deleted)} 個）" if deleted else ""))
        finally:
            self._after_delete(series_id, deleted + ([folder["cid"]] if folder_removed else []), folder_removed)
        log.info("刪除「%s」的 %s 集（移到 115 回收站）%s", plan["name"], len(deleted), "，劇集資料夾也移到回收站" if folder_removed else "")
        return {"deleted": len(deleted), "folder_removed": folder_removed, "note": note}

    def delete_series(self, series_id: int) -> dict:
        """整部劇刪掉：劇集資料夾整個移到 115 回收站（裡面所有檔案，包括沒進媒體庫的），本機和媒體庫跟著拿掉。"""
        series, task, rel, cid, path = self._series_folder(series_id)
        if not cid:
            raise ReorgError("這部劇的資料夾就是同步目錄本身（或同步紀錄裡沒有），不能整個刪；請勾要刪的集")
        self._begin_delete()
        try:
            if self._folder_path(cid) != path:
                raise ReorgError("劇集資料夾已經不在原本的位置（可能在 115 上搬過），請先同步一次")
            self.p115.delete_files([cid])
        except P115Error as exc:
            self._lock.release()
            raise ReorgError(f"115 刪除失敗：{exc}")
        except ReorgError:
            self._lock.release()
            raise
        self._after_delete(series_id, [cid], True)
        log.info("整部「%s」移到 115 回收站：%s", series["name"], path)
        return {"deleted": 1, "folder_removed": True, "note": ""}

    def delete_item(self, file_id: int, is_dir: bool, path: str, local: Optional[str]) -> dict:
        """一個資料夾或檔案移到 115 回收站（電影、瀏覽 115 裡的資料夾），本機和媒體庫跟著拿掉。
        資料夾先確認 id 還在原本的路徑；local 是本機對應的位置（不在同步目錄裡是 None）。"""
        self._begin_delete()
        try:
            if is_dir and self._folder_path(file_id) != path.rstrip("/"):
                raise ReorgError("資料夾已經不在原本的位置（可能在 115 上搬過），請重新整理清單")
            self.p115.delete_files([file_id])
        except (P115Error, ReorgError) as exc:
            self._lock.release()
            raise exc if isinstance(exc, ReorgError) else ReorgError(f"115 刪除失敗：{exc}")
        self._after_delete(None, [file_id], False, extra=[local] if local else [])
        log.info("移到 115 回收站：%s", path)
        return {"deleted": 1, "folder_removed": is_dir, "note": ""}

    def delete_in_folder(self, parent_cid: int, ids: List[int]) -> dict:
        """瀏覽 115 裡勾的資料夾、檔案移到 115 回收站（資料夾連同裡面所有檔案），同步目錄裡的本機 strm、nfo 和媒體庫跟著拿掉。
        先列一次這個資料夾，只刪真的在裡面的（清單過期、id 不對的不刪）。"""
        ids = list(dict.fromkeys(int(i) for i in ids))
        if not ids:
            raise ReorgError("沒有勾要刪的")
        self._begin_delete()
        deleted: List[int] = []
        local_dirs: List[str] = []
        try:
            entries = {e["id"]: e for e in self.p115.list_dir(parent_cid)}
            pick = [i for i in ids if i in entries]
            if len(pick) != len(ids):
                raise ReorgError("勾的有些已經不在這個資料夾裡（清單可能已經過期），請重新整理清單")
            local_dirs = self._local_dirs([i for i in pick if entries[i]["is_dir"]])
            for start in range(0, len(pick), DELETE_BATCH):
                batch = pick[start:start + DELETE_BATCH]
                self.p115.delete_files(batch)
                deleted += batch
        except (P115Error, ReorgError) as exc:
            self._after_delete(None, deleted, False, extra=local_dirs if deleted else [])
            if isinstance(exc, ReorgError):
                raise
            raise ReorgError(f"115 刪除失敗：{exc}" + (f"（已經刪了 {len(deleted)} 個）" if deleted else ""))
        self._after_delete(None, deleted, False, extra=local_dirs)
        log.info("瀏覽 115：%s 個移到 115 回收站：%s", len(deleted), "、".join(entries[i]["name"] for i in deleted[:5]))
        return {"deleted": len(deleted), "names": [entries[i]["name"] for i in deleted]}

    def _local_dirs(self, dir_ids: List[int]) -> List[str]:
        """這些 115 資料夾在本機對應的位置（同步紀錄裡有的），刪掉後給媒體庫重新掃描。"""
        out = []
        for task in self.strm_sync.tasks:
            local = Path(task.local).expanduser()
            for i in dir_ids:
                row = self.db.one("SELECT path FROM p115_index WHERE task=? AND file_id=? AND is_dir=1", (task_key(task), i))
                if row:
                    out.append(str(local / row["path"]))
        return out

    def _after_delete(self, series_id: Optional[int], ids: List[int], folder_removed: bool, extra: Optional[List[str]] = None) -> None:
        """115 上刪了：本機 strm、nfo 拿掉，重新掃描那些位置（劇集資料夾刪了就連劇一起拿掉）；最後放開鎖。"""
        try:
            removed = self.strm_sync.remove_local(ids) if ids else []
            series = self.db.one("SELECT path FROM items WHERE id=?", (series_id,)) if series_id else None
            paths = removed + ([series["path"]] if folder_removed and series else []) + (extra or [])
            if paths and self.scanner:
                self.scanner.scan_paths(paths)
        finally:
            self._lock.release()

    def remember_preview(self, token: str, payload: dict) -> None:
        """別的地方（整理 115 網盤）做好的預覽，交給這裡照它執行。"""
        now = time.time()
        self._previews = {k: v for k, v in self._previews.items() if now - v["at"] < PREVIEW_TTL}
        self._previews[token] = {**payload, "at": now}

    def execute_in_background(self, tokens: List[str], cleanup: Optional[List[dict]] = None) -> None:
        """照一個或幾個預覽代碼執行。cleanup：[{cid, path}]，整理完沒有影片留下就移到 115 回收站；
        只能是這次預覽的來源資料夾。"""
        now = time.time()
        previews: List[dict] = []
        for token in tokens:
            pv = self._previews.get(str(token or ""))
            if not pv or now - pv["at"] > PREVIEW_TTL:
                raise ReorgError("預覽已經過期或不存在，請重新預覽")
            previews.append(pv)
        if not previews:
            raise ReorgError("沒有要執行的預覽")
        folders = {pv.get("cid") for pv in previews if pv.get("cid")}
        cleanup = [{"cid": int(c["cid"]), "path": str(c.get("path") or "")} for c in cleanup or []]
        if any(c["cid"] not in folders for c in cleanup):
            raise ReorgError("只能清掉這次整理的來源資料夾")
        if not self._lock.acquire(blocking=False):
            raise ReorgError("已經有一批在整理或刪除，等它完成再執行")
        for token in tokens:
            self._previews.pop(str(token), None)
        total = sum(b["count"] for pv in previews for b in pv["batches"])
        title = previews[0]["title"] if len(previews) == 1 else f"整理 {len(previews)} 個資料夾"
        self.job = ReorgJob(running=True, started=time.time(), title=title, total=total)
        threading.Thread(target=self._run, args=(previews, cleanup), daemon=True).start()

    def _run(self, previews: List[dict], cleanup: List[dict]) -> None:
        job = self.job
        try:
            for pv in previews:
                for batch in pv["batches"]:
                    self._run_items(pv, batch, job)
            log.info("MoviePilot 整理 %s：%s 個完成，%s 個失敗", job.title, job.done, job.failed)
            if cleanup:
                self._remove_empty_folders(cleanup, job)
            if job.done:
                job.current = f"等 115 記下變動，{int(self.sync_delay)} 秒後同步"
                time.sleep(self.sync_delay)
                job.synced = "started" if self.strm_sync.run_in_background(INCREMENTAL) else "busy"
        except Exception as exc:  # 背景執行緒：記下來，不讓整個工作卡在「執行中」
            job.errors.append(f"{type(exc).__name__}: {exc}")
            log.exception("整理時發生錯誤")
        finally:
            job.current = ""
            job.running = False
            job.finished = time.time()
            self._lock.release()

    def _run_items(self, pv: dict, batch: dict, job: ReorgJob) -> None:
        """一批：一個資料夾或幾個檔案交給 MoviePilot，照預覽時的指定（沒指定的是 None，讓它自己認）；結果照它回的每個檔案記。"""
        job.current = f"MoviePilot 整理中：{batch['label']}（{batch['count']} 個檔案）"
        try:
            results = self.mp.transfer(batch["fileitems"], batch.get("tmdbid") or None, batch.get("season"),
                                       batch.get("episode_format") or None, pv["scrape"], pv["target_path"], preview=False,
                                       mtype=batch.get("type_name"), timeout=max(600, 60 * batch["count"]),
                                       single=batch["single"])
        except MoviePilotError as exc:
            job.errors.append(f"{batch['label']}：{exc}")
            job.failed += batch["count"]
            log.warning("MoviePilot 整理 %s 失敗：%s", batch["label"], exc)
            return
        done = 0
        for r in results:
            state = str(r.get("state") or ("completed" if r.get("success") else "failed"))
            job.items.append({"name": posixpath.basename(str(r.get("source") or "").rstrip("/")), "state": state,
                              "target": r.get("target") or r.get("target_dir") or "", "message": r.get("message") or ""})
            if state in ("completed", "accepted"):
                done += 1
            else:
                job.failed += 1
        job.done += done
        if done and batch.get("local"):  # 本機跟著搬的 strm 旁邊，寫著 -1 的舊 nfo 刪掉，免得蓋過 MoviePilot 新刮的
            for strm in Path(batch["local"]).rglob("*.strm"):
                self._drop_stale_nfo(strm, strict=True)

    def _remove_empty_folders(self, folders: List[dict], job: ReorgJob) -> None:
        """整理完的來源資料夾沒有影片留下就移到 115 回收站（可以還原）；還有影片（整理失敗、目標已有同一集）就留著。"""
        for c in folders:
            path = c["path"] or f"資料夾 {c['cid']}"
            job.current = f"檢查舊資料夾 {path}"
            try:
                if c["path"] and self._folder_path(c["cid"]) != c["path"].rstrip("/"):
                    # MoviePilot 搬空後可能已經自己刪了；id 對不上原本的位置就不動它
                    job.items.append({"name": path, "state": "kept", "target": "",
                                      "message": "資料夾已經不在原本的位置（可能 MoviePilot 搬完已經刪掉），沒有動它"})
                    continue
                left = self._videos_left(c["cid"])
                if left:
                    job.items.append({"name": path, "state": "kept", "target": "",
                                      "message": f"還有 {left} 支影片（整理失敗或目標已有同一集的會留在原處），資料夾保留"})
                    continue
                self.p115.delete_files([c["cid"]])
                job.items.append({"name": path, "state": "removed", "target": "", "message": "沒有影片留下，已移到 115 回收站"})
                log.info("整理後沒有影片留下的舊資料夾移到 115 回收站：%s", path)
            except P115Error as exc:
                job.items.append({"name": path, "state": "kept", "target": "", "message": f"資料夾保留：{exc}"})

    def _folder_path(self, cid: int) -> str:
        """115 上這個資料夾 id 現在的完整路徑；已經不存在時是空字串。"""
        try:
            return "/" + "/".join(name for _, name in self.p115.dir_ancestors(cid))
        except P115Error:
            return ""

    def _videos_left(self, cid: int) -> int:
        """資料夾（含子資料夾）裡還有幾支影片。"""
        delay = getattr(self.strm_sync.cfg, "request_delay", 0) or 0
        left, stack = 0, [cid]
        while stack:
            for e in self.p115.list_dir(stack.pop()):
                if e["is_dir"]:
                    stack.append(e["id"])
                elif posixpath.splitext(e["name"])[1].lower() in VIDEO_EXTS:
                    left += 1
            if stack and delay:
                time.sleep(delay)
        return left

    @staticmethod
    def _drop_stale_nfo(strm: Path, strict: bool = False) -> None:
        """刪掉本機沒用的舊 nfo，免得同步時它跟著 strm 改成新名字；MoviePilot 刮削的新 nfo 會在同步時下載。

        一季整理的集本來就沒有可用的集號：nfo 沒有集號（或寫 -1）就刪。整理資料夾時可能是電影，
        只刪寫著負數季、集號的（刮削時沒認出來的那種）。
        """
        nfo = strm.with_suffix(".nfo")
        try:
            if not nfo.is_file():
                return
            bad = NEGATIVE_NUMBER_RE.search(nfo.read_text(encoding="utf-8", errors="replace")) if strict \
                else "index_number" not in parse_nfo(nfo)
            if bad:
                nfo.unlink()
                log.info("刪掉沒有集號的舊 nfo：%s", nfo)
        except OSError as exc:
            log.warning("刪不掉舊 nfo %s：%s", nfo, exc)
