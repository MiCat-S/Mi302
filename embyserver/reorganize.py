"""集號不對的劇交給 MoviePilot 整理。

刮削時沒認出集號的集（nfo 沒有集號或寫 -1），檔名又不是 S01E02 這種標準寫法，Mi302 只能從「10.xxx」
「第10集」這類寫法猜，或根本認不出來。交給 MoviePilot 的
「手動整理」：它在 115 上把檔案改成自己命名設定的標準名稱（例如「劇名 - S01E10 - 集名」）並刮削，
Mi302 再用增量同步把本機的 strm 跟著搬過去。以後哪個工具看檔名都認得出是第幾集。

會真的改 115 上的檔名和位置，所以一定先預覽：
1. 清單（candidates）：集號是猜的、或認不出來的集，一季一列。整理後檔名變成標準寫法，就不再列。
2. 計畫（plan）：每一集對應到 115 上的哪個檔案。依 Mi302 在檔名裡找到集號的位置，產生 MoviePilot 的
   「集數定位」模板（「10.潘玮柏…」是 {ep}.{a}、「03-比赛…」是 {ep}-{a}），寫法一樣的放同一批；
   EP02、第十二集這類 MoviePilot 自己認得的不給模板；認不出集號的請 MoviePilot 推薦。
3. 預覽（preview）：先確認 MoviePilot 是 v2.11.1-1 以上（更舊的不認預覽，會直接整理），
   再請它只算不做，列出每個檔案的新路徑。Mi302 另外檢查它認的集號和檔名的
   一不一樣、會不會搬到別的劇集資料夾、會不會搬出同步目錄（搬出去的不能執行，否則會從媒體庫消失）。
4. 執行（execute）：只送預覽成功的檔案，參數必須和預覽時一模一樣（用預覽代碼對應，半小時內有效）。
   完成後刪掉本機寫著 -1 的舊 nfo（免得同步時它跟著 strm 搬到新名字），等一下再跑增量同步。

也可以整理「瀏覽 115」裡選的任何一個資料夾（folder_plan）：資料夾裡（含子資料夾）的影片，最多 500 支。
類型、TMDB 編號、季都可以不填，讓 MoviePilot 自己辨識；檔名看不出集號的照樣送（可能是電影）。
原本在同步目錄裡的檔案不能搬出同步目錄；原本不在的只提醒。
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
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .browse115 import library_info
from .config import StrmTask
from .db import Database
from .filetypes import VIDEO_EXTS
from .moviepilot import MoviePilot, MoviePilotError
from .p115 import P115Error
from .scanner import episode_match, parse_episode, parse_nfo, parse_season_dir
from .strm_sync import INCREMENTAL, remote_root, task_key
from .textutil import title_match

log = logging.getLogger(__name__)

# EPISODE_PATTERNS 裡要告訴 MoviePilot 集號在哪的寫法：第N集、開頭就是集號。SxxEyy、1x02、EP02 它自己認得
TEMPLATE_PATTERNS = {2, 4, 5}
PREVIEW_TTL = 1800  # 預覽多久內可以執行
NATIVE, UNKNOWN = "native", "unknown"  # 不用模板的一批、認不出集號的一批
EP_TEXT_RE = re.compile(r"^([Ee][Pp]?)?(\d{1,4})(-([Ee][Pp]?)?(\d{1,4}))?$")  # MoviePilot 對 {ep} 內容的要求
FOLDER_LIMIT = 500  # 整理一個資料夾最多幾支影片
TMDB_IN_NAME_RE = re.compile(r"tmdb(?:id)?\s*[=:\-_]\s*(\d+)", re.I)  # 資料夾名稱裡的 [tmdb=103863]、{tmdb-103863}
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


def candidates(db: Database, query: str = "", offset: int = 0, limit: int = 20) -> Tuple[List[dict], int]:
    """集號是從不標準的檔名猜的、或認不出來的 strm 集，一季一列。"""
    where = ["e.type='Episode'", "e.is_strm=1", "e.ep_from IN ('name','none')"]
    params: list = []
    if query.strip():
        sql, more = title_match(query, "s.name", "s.original_title", "s.search_text")
        where.append(sql)
        params += more
    base = (f"FROM items e JOIN items s ON s.id=e.series_id WHERE {' AND '.join(where)} "
            "GROUP BY e.series_id, e.parent_index_number")
    total = db.one(f"SELECT COUNT(*) AS c FROM (SELECT 1 {base})", params)["c"]
    rows = db.query(
        "SELECT e.series_id, e.parent_index_number AS season, s.name, s.year, COUNT(*) AS episodes, "
        "SUM(e.ep_from='none') AS unknown, (SELECT COUNT(*) FROM items x WHERE x.series_id=e.series_id "
        "AND x.parent_index_number=e.parent_index_number AND x.type='Episode') AS season_total "
        f"{base} ORDER BY s.sort_name, s.id, season LIMIT ? OFFSET ?", (*params, limit, offset),
    )
    return [dict(r) for r in rows], total


@dataclass
class _File:
    file_id: int
    name: str  # 115 上的檔名
    remote: str  # 115 上的完整路徑
    pickcode: str
    size: int
    parent_id: int
    local: Optional[str]  # 本機的 strm；不在同步任務裡的是 None
    episode: Optional[int]  # Mi302 從檔名認出的集號


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
    def __init__(self, db: Database, strm_sync, moviepilot: MoviePilot):
        self.db = db
        self.strm_sync = strm_sync
        self.mp = moviepilot
        self.job = ReorgJob()
        self.sync_delay = 20.0  # 等 115 記下這次的移動、改名，再跑增量同步
        self._lock = threading.Lock()
        self._plans: Dict[str, dict] = {}  # 計畫代碼（s{劇}-{季}、f{資料夾 id}）→ 計畫
        self._previews: Dict[str, dict] = {}

    @property
    def p115(self):
        return self.strm_sync.p115

    def ready(self) -> dict:
        """缺什麼就不能整理：MoviePilot 的帳號密碼（手動整理只接受帳號登入）、115 登入。"""
        return {"moviepilot": self.mp.enabled, "login": self.mp.can_subscribe, "p115": self.p115.logged_in}

    # ---------------- 計畫 ----------------

    def _locate(self, local: str) -> Tuple[Optional[StrmTask], str]:
        """本機路徑在哪個同步任務的資料夾裡，以及相對於那個資料夾的路徑；不在任何任務裡回傳 (None, "")。"""
        for task in self.strm_sync.tasks:
            try:
                return task, Path(local).relative_to(Path(task.local).expanduser()).as_posix()
            except ValueError:
                continue
        return None, ""

    @staticmethod
    def _item(f: _File) -> dict:
        """MoviePilot 的 115 檔案項目；它的 115 存儲搬檔案要靠 fileid。"""
        stem, ext = posixpath.splitext(f.name)
        return {"storage": "u115", "type": "file", "path": f.remote, "name": f.name, "basename": stem,
                "extension": ext.lstrip(".").lower(), "size": f.size, "fileid": str(f.file_id),
                "parent_fileid": str(f.parent_id), "pickcode": f.pickcode}

    def _group(self, files: List[_File], recommend: bool) -> List[dict]:
        """依檔名寫法分批：集號位置一樣的一批（附集數定位模板）、MoviePilot 自己認得的、認不出集號的。"""
        groups: Dict[str, dict] = {}
        for f in files:
            template = episode_template(f.name) if f.episode is not None else None
            key = UNKNOWN if f.episode is None else template or NATIVE
            groups.setdefault(key, {"key": key, "template": template or "", "source": "mi302" if template else "",
                                    "note": "", "files": []})["files"].append(f)
        if NATIVE in groups:
            groups[NATIVE]["note"] = "檔名裡有 S01E02、EP02、第十二集這類集號，MoviePilot 自己認得，不用集數定位"
        if UNKNOWN in groups:
            if recommend:
                template, why = self.mp.recommend_format([self._item(f) for f in groups[UNKNOWN]["files"]]) \
                    if self.mp.can_subscribe else (None, "")
                groups[UNKNOWN].update(template=template or "", source="moviepilot" if template else "",
                                       note=f"Mi302 認不出集號，模板是 MoviePilot 推薦的（{why}）" if template else
                                       "Mi302 和 MoviePilot 都認不出集號；要整理的話自己填集數定位，例如 {ep}.{a}")
            else:
                groups[UNKNOWN]["note"] = "檔名看不出集號（電影通常是這樣）：交給 MoviePilot 自己辨識；是劇集的話可以填集數定位"
        return sorted(groups.values(), key=lambda g: (g["key"] == UNKNOWN, g["key"] == NATIVE, g["key"]))

    def _save_plan(self, plan_id: str, mode: str, files: List[_File], groups: List[dict], **extra) -> None:
        self._plans[plan_id] = {"mode": mode, "files": {f.file_id: f for f in files},
                                "groups": {g["key"]: g for g in groups}, **extra}

    @staticmethod
    def _groups_view(groups: List[dict], unknown_default: bool) -> List[dict]:
        return [
            {"key": g["key"], "template": g["template"], "source": g["source"], "note": g["note"],
             "enabled": g["key"] != UNKNOWN or bool(g["template"]) or unknown_default,
             "files": [{"file_id": f.file_id, "name": f.name, "episode": f.episode} for f in g["files"]]}
            for g in groups
        ]

    def plan(self, series_id: int, season: int) -> dict:
        """一部劇的一季：集號是猜的、或認不出的集。"""
        series = self.db.one("SELECT id, name, year, path, provider_ids FROM items WHERE id=? AND type='Series'",
                             (series_id,))
        if not series:
            raise ReorgError("找不到這部劇")
        episodes = self.db.query(
            "SELECT path, index_number FROM items WHERE series_id=? AND parent_index_number=? AND type='Episode' "
            "AND is_strm=1 AND ep_from IN ('name','none') ORDER BY index_number, path", (series_id, season),
        )
        if not episodes:
            raise ReorgError("這一季沒有要整理的集")
        task, series_rel = self._locate(series["path"])
        if not task:
            raise ReorgError("這部劇不在任何 115 同步任務的本機資料夾裡，MoviePilot 找不到 115 上的檔案")
        remote_series = posixpath.join(remote_root(task), series_rel) if series_rel != "." else remote_root(task)
        files: List[_File] = []
        missing: List[dict] = []
        listings: Dict[str, Tuple[int, Dict[int, dict]]] = {}
        try:
            for ep in episodes:
                task, rel = self._locate(ep["path"])
                row = self.db.one("SELECT file_id FROM p115_index WHERE task=? AND path=? AND is_dir=0",
                                  (task_key(task), rel)) if task else None
                if not row:
                    missing.append({"path": ep["path"], "reason": "115 同步的紀錄裡沒有這個 strm，先跑一次全量同步"})
                    continue
                folder = posixpath.dirname(rel)
                remote_dir = posixpath.join(remote_root(task), folder) if folder else remote_root(task)
                if remote_dir not in listings:
                    drow = self.db.one("SELECT file_id FROM p115_index WHERE task=? AND path=? AND is_dir=1",
                                       (task_key(task), folder)) if folder else None
                    cid = drow["file_id"] if drow else self.p115.dir_id(remote_dir)
                    listings[remote_dir] = (cid, {e["id"]: e for e in self.p115.list_dir(cid) if not e["is_dir"]})
                cid, entries = listings[remote_dir]
                info = entries.get(row["file_id"])
                if not info:
                    missing.append({"path": ep["path"], "reason": "115 上已經沒有這個檔案（搬走或刪掉了），先同步一次"})
                    continue
                files.append(_File(row["file_id"], info["name"], posixpath.join(remote_dir, info["name"]),
                                   info.get("pickcode") or "", int(info.get("size") or 0), cid, ep["path"],
                                   ep["index_number"]))
        except P115Error as exc:
            raise ReorgError(f"讀不到 115 上的檔案：{exc}")
        groups = self._group(files, recommend=True)
        try:
            tmdbid = (json.loads(series["provider_ids"] or "{}") or {}).get("Tmdb") or ""
        except ValueError:
            tmdbid = ""
        plan_id = f"s{series_id}-{season}"
        self._save_plan(plan_id, "season", files, groups, title=f"{series['name']} 第 {season} 季", season=season,
                        remote_series=remote_series, remote_parent=posixpath.dirname(remote_series))
        return {
            "plan_id": plan_id, "mode": "season", "series_id": series_id, "name": series["name"], "year": series["year"],
            "season": season, "tmdbid": str(tmdbid), "type": "tv", "remote_series": remote_series,
            "remote_parent": posixpath.dirname(remote_series), "groups": self._groups_view(groups, False),
            "missing": missing,
        }

    def folder_plan(self, cid: int, path: str) -> dict:
        """115 上的一個資料夾（含子資料夾）裡的影片，最多 FOLDER_LIMIT 支。"""
        path = "/" + path.strip("/") if path.strip("/") else "/"
        if not cid or path == "/":
            raise ReorgError("不能整理整個 115，請選一個資料夾")
        delay = getattr(self.strm_sync.cfg, "request_delay", 0) or 0
        files: List[_File] = []
        stack = [(cid, path)]
        try:
            while stack:
                folder_id, folder = stack.pop()
                entries = self.p115.list_dir(folder_id)
                for e in sorted(entries, key=lambda e: e["name"].lower(), reverse=True):
                    if e["is_dir"]:
                        stack.append((e["id"], posixpath.join(folder, e["name"])))
                    elif posixpath.splitext(e["name"])[1].lower() in VIDEO_EXTS:
                        if len(files) >= FOLDER_LIMIT:
                            raise ReorgError(f"這個資料夾的影片超過 {FOLDER_LIMIT} 支，請選小一點的資料夾（例如一部劇）")
                        files.append(_File(e["id"], e["name"], posixpath.join(folder, e["name"]),
                                           e.get("pickcode") or "", int(e.get("size") or 0), folder_id, None,
                                           parse_episode(posixpath.splitext(e["name"])[0])[1]))
                if stack and delay:
                    time.sleep(delay)
        except P115Error as exc:
            raise ReorgError(f"讀不到 115 上的檔案：{exc}")
        if not files:
            raise ReorgError("這個資料夾裡沒有影片")
        files.sort(key=lambda f: f.remote)
        lib = library_info(self.db, self.strm_sync.tasks, [f.file_id for f in files])
        for f in files:
            f.local = (lib.get(f.file_id) or {}).get("local")
        kinds = {(lib.get(f.file_id) or {}).get("type") for f in files}
        mtype = "tv" if "Episode" in kinds else "movie" if "Movie" in kinds else \
            "tv" if any(f.episode is not None for f in files) else "auto"
        tmdbid = ""
        for info in lib.values():  # 媒體庫裡已經刮削過：用它的 tmdbid
            try:
                tmdbid = (json.loads(info.get("provider_ids") or "{}") or {}).get("Tmdb") or ""
            except ValueError:
                tmdbid = ""
            if tmdbid:
                break
        if not tmdbid:  # 資料夾名稱裡的 [tmdb=103863]、{tmdb-103863}
            for part in reversed(path.split("/")):
                m = TMDB_IN_NAME_RE.search(part)
                if m:
                    tmdbid = m.group(1)
                    break
        season = parse_season_dir(posixpath.basename(path))
        seasons = {(lib.get(f.file_id) or {}).get("season") for f in files if lib.get(f.file_id)}
        if season is None and len(seasons) == 1 and None not in seasons:
            season = seasons.pop()
        groups = self._group(files, recommend=False)
        plan_id = f"f{cid}"
        in_sync = sum(1 for f in files if f.local)
        self._save_plan(plan_id, "folder", files, groups, title=path, remote_parent=posixpath.dirname(path))
        return {
            "plan_id": plan_id, "mode": "folder", "path": path, "name": posixpath.basename(path),
            "remote_parent": posixpath.dirname(path), "tmdbid": str(tmdbid), "type": mtype, "season": season,
            "in_sync": in_sync, "groups": self._groups_view(groups, True), "missing": [],
        }

    # ---------------- 預覽 ----------------

    def preview(self, plan_id: str, tmdbid: str, mtype: str, season: Optional[int], target: str, target_path: str,
                scrape: bool, groups: List[dict]) -> dict:
        plan = self._plans.get(plan_id)
        m = re.fullmatch(r"s(\d+)-(\d+)", plan_id)
        if not plan and m:  # 一季的計畫可以重做（例如伺服器重新啟動過）
            self.plan(int(m.group(1)), int(m.group(2)))
            plan = self._plans.get(plan_id)
        if not plan:
            raise ReorgError("這個計畫已經過期，請關掉對話框重新打開")
        folder = plan["mode"] == "folder"
        tmdbid = str(tmdbid or "").strip()
        if tmdbid and not tmdbid.isdigit():
            raise ReorgError("TMDB 編號要是數字")
        if not folder and not tmdbid:
            raise ReorgError("請填 TMDB 編號（數字）")
        type_name = {"tv": "电视剧", "movie": "电影"}.get(mtype) if folder else "电视剧"
        if not folder:
            season = plan["season"]
        if target == "parent":
            target_path = plan["remote_parent"]
        elif target == "path":
            target_path = "/" + str(target_path or "").strip().strip("/")
            if target_path == "/":
                raise ReorgError("請填要整理到哪個 115 資料夾")
        else:
            target_path = None
        try:
            self.mp.check_transfer_preview()  # 舊版 MoviePilot 會把預覽當成真的整理
        except MoviePilotError as exc:
            raise ReorgError(str(exc))
        roots = [remote_root(t) for t in self.strm_sync.tasks]
        items: List[dict] = []
        batches: List[dict] = []
        notes: List[str] = []
        for g in groups:
            pg = plan["groups"].get(g.get("key"))
            if not pg or not g.get("enabled"):
                continue
            template = None if type_name == "电影" else str(g.get("template") or "").strip() or None
            if pg["key"] == UNKNOWN and not template and not folder:
                notes.append(f"認不出集號的 {len(pg['files'])} 個檔案沒有集數定位模板，這次不送")
                continue
            send: List[_File] = []
            for f in pg["files"]:
                if template and template_episode(template, f.name) is None:
                    items.append(self._view(f, {}, template, plan, roots, "集數定位模板對不上這個檔名，沒有送出"))
                else:
                    send.append(f)
            if not send:
                continue
            try:
                results = self.mp.transfer([self._item(f) for f in send], tmdbid or None, season, template, scrape,
                                           target_path, preview=True, mtype=type_name, timeout=max(60, 10 * len(send)))
            except MoviePilotError as exc:
                raise ReorgError(f"MoviePilot 預覽失敗：{exc}")
            by_source = {r.get("source"): r for r in results}
            ok = []
            for f in send:
                view = self._view(f, by_source.get(f.remote) or {}, template, plan, roots)
                items.append(view)
                if view["ok"]:
                    ok.append(f.file_id)
            if ok:
                batches.append({"template": template, "file_ids": ok})
        payload = {"plan_id": plan_id, "tmdbid": tmdbid, "type_name": type_name, "season": season,
                   "target_path": target_path, "scrape": bool(scrape), "batches": batches}
        token = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16] if batches else None
        now = time.time()
        self._previews = {k: v for k, v in self._previews.items() if now - v["at"] < PREVIEW_TTL}
        if token:
            self._previews[token] = {**payload, "at": now, "title": plan["title"], "mode": plan["mode"],
                                     "files": plan["files"]}
        return {
            "token": token, "items": items, "notes": notes,
            "summary": {"total": len(items), "ok": sum(1 for i in items if i["ok"]),
                        "failed": sum(1 for i in items if not i["ok"]),
                        "warnings": sum(1 for i in items if i["ok"] and i["warnings"])},
        }

    @staticmethod
    def _view(f: _File, r: dict, template: Optional[str], plan: dict, roots: List[str], fail: str = "") -> dict:
        """一個檔案的預覽結果，加上 Mi302 自己的檢查。"""
        target = str(r.get("target") or r.get("target_dir") or "")
        expected = template_episode(template, f.name) if template else f.episode
        episode = int(r["episode"]) if str(r.get("episode") or "").isdigit() else None
        ok, message, warnings = bool(r.get("success")) and bool(target) and not fail, fail or r.get("message") or "", []
        season_mode = plan["mode"] == "season"
        inside = any(target.startswith(root.rstrip("/") + "/") for root in roots)
        if ok and not inside:
            if season_mode or f.local:
                ok, message = False, "新位置不在 Mi302 的 115 同步目錄裡，整理後會從媒體庫消失，所以不能執行"
            else:
                warnings.append("新位置不在 Mi302 的 115 同步目錄裡，Mi302 不會替它產生 strm")
        if ok and season_mode and not target.startswith(plan["remote_series"].rstrip("/") + "/"):
            warnings.append("會搬到別的劇集資料夾，不是現在的「" + posixpath.basename(plan["remote_series"]) + "」")
        if ok and expected is not None and episode is not None and episode != expected:
            warnings.append(f"MoviePilot 認成第 {episode} 集，檔名看起來是第 {expected} 集")
        if ok and season_mode and episode is None:
            warnings.append("MoviePilot 沒有說是第幾集")
        if not ok and not message:
            message = "MoviePilot 沒有說明原因"
        return {"file_id": f.file_id, "name": f.name, "source": f.remote, "target": target, "episode": episode,
                "expected": expected, "ok": ok, "message": message, "warnings": warnings}

    # ---------------- 執行 ----------------

    def execute_in_background(self, token: str) -> None:
        pv = self._previews.get(token or "")
        if not pv or time.time() - pv["at"] > PREVIEW_TTL:
            raise ReorgError("預覽已經過期或不存在，請重新預覽")
        if not self._lock.acquire(blocking=False):
            raise ReorgError("已經有一批在整理，等它完成再執行")
        self._previews.pop(token, None)
        total = sum(len(b["file_ids"]) for b in pv["batches"])
        self.job = ReorgJob(running=True, started=time.time(), title=pv["title"], total=total)
        threading.Thread(target=self._run, args=(pv,), daemon=True).start()

    def _run(self, pv: dict) -> None:
        job = self.job
        moved: List[_File] = []
        try:
            for batch in pv["batches"]:
                files = [pv["files"][i] for i in batch["file_ids"]]
                job.current = f"MoviePilot 整理中（{len(files)} 個檔案）"
                try:
                    results = self.mp.transfer([self._item(f) for f in files], pv["tmdbid"] or None, pv["season"],
                                               batch["template"], pv["scrape"], pv["target_path"], preview=False,
                                               mtype=pv["type_name"], timeout=max(300, 60 * len(files)))
                except MoviePilotError as exc:
                    job.errors.append(str(exc))
                    job.failed += len(files)
                    log.warning("MoviePilot 整理失敗：%s", exc)
                    continue
                by_source = {r.get("source"): r for r in results}
                for f in files:
                    r = by_source.get(f.remote) or {}
                    state = str(r.get("state") or ("completed" if r.get("success") else "failed"))
                    job.items.append({"name": f.name, "state": state, "target": r.get("target") or "",
                                      "message": r.get("message") or ""})
                    if state in ("completed", "accepted"):
                        job.done += 1
                        moved.append(f)
                    else:
                        job.failed += 1
            for f in moved:
                if f.local:
                    self._drop_stale_nfo(Path(f.local), strict=pv["mode"] == "folder")
            self._plans.pop(pv["plan_id"], None)
            log.info("MoviePilot 整理 %s：%s 個完成，%s 個失敗", job.title, job.done, job.failed)
            if moved:
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
