"""115 上重複的影片：找出來、建議保留哪一份、刪掉多的。

兩種重複，一次「找重複」同時找：
- 完全相同（exact）：SHA1 和大小都一樣。115 列檔案時本來就附上 SHA1，四萬多個檔案大約四十次請求。
  建議保留本機有 strm 的那份（刮削資料、觀看紀錄都在它身上），其次檔名編號格式完整的，再來最早上傳的；
  其餘預先勾選。
- 不同版本（versions）：媒體庫裡同一部電影（tmdbid，沒有就片名＋年份）或同一部劇的同一集，檔案卻不同，
  例如 1080p 和 2160p。只看同步任務裡、有 strm 的檔案。不知道第幾集的、檔名集號和媒體庫對不上的不比；
  分段檔（CD1、Part 2）、一個檔案好幾集的不算；
  導演剪輯版、加長版這類版本名不同的分開算。建議保留哪一份看使用者選的偏好（預設 1080P 優先，沒有再 4K，
  再沒有就留剩下最高的），解析度一樣時保留檔名編號格式完整的，再來檔案小的（省空間）；預設不勾，由使用者挑。
  改偏好時已找到的結果當場重算。

檔名編號格式完整：劇集要有 S01E02（或 1x02）這種季和集都寫明的編號，電影要有年份。

刪除：送進 115 回收站（在 115 還原得回來），每組至少留一份。本機的 strm 和同名的中繼資料跟著刪，
觀看紀錄轉到保留的那份，再重新掃描受影響的劇或電影。刪過的記在 dup_deleted，方便到回收站找回。
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .db import Database
from .filetypes import VIDEO_EXTS
from .mediainfo import MediaInfoStore
from .p115 import P115Error, P115Service
from .scanner import STANDARD_EPISODE, episode_match, parse_episode
from .textutil import simplified

log = logging.getLogger(__name__)

SCAN_META_KEY = "dupes_scan"
# 「不同版本」的判斷規則改過時加一：舊規則找出的結果可能不對，啟動時清掉，請使用者重新找
VERSIONS_RULE = "2"
VERSIONS_RULE_KEY = "dupes_versions_rule"
VERSIONS_OUTDATED_KEY = "dupes_versions_outdated"
# 不同版本建議保留哪種解析度：1080 = 1080P 優先（沒有再 4K）、2160 = 4K 優先（沒有再 1080P）、highest = 最高的
PREFER_KEY = "dupes_prefer"
PREFER_APPLIED_KEY = "dupes_prefer_applied"  # 資料庫裡的建議是照哪個偏好算的
# 建議保留的規則改過時加一：啟動時照新規則重算已找到的結果（不用重新找）
SUGGEST_RULE = "2"  # 2：比檔名編號格式完不完整，同解析度保留小的
SUGGEST_RULE_KEY = "dupes_suggest_rule"
YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
PREFERS = ("1080", "2160", "highest")
DEFAULT_PREFER = "1080"


def res_rank(res: Optional[int], prefer: str) -> Tuple[int, int]:
    """解析度的排序鍵（小的優先）：偏好的解析度 → 比它高的（由低到高）→ 比它低的（由高到低）→ 看不出來的。"""
    if not res:
        return (3, 0)
    if prefer not in ("1080", "2160"):
        return (0, -res)
    want = int(prefer)
    if res == want:
        return (0, 0)
    return (1, res) if res > want else (2, -res)


def name_complete(name: str, kind: Optional[str] = None) -> bool:
    """檔名的編號格式完不完整。kind：episode = 要有 S01E02、1x02 這種季和集都寫明的；movie = 要有年份。

    不知道是劇集還是電影時（完全相同的檔案）：看得出集號的要是標準寫法；看不出集號的要有年份。
    只寫了集號的（EP02、第2集、10.xxx）都算不完整。
    """
    stem = posixpath.splitext(name)[0]
    found = episode_match(stem)
    standard = bool(found) and found[1] in STANDARD_EPISODE
    if kind == "episode" or (kind is None and found):
        return standard
    return bool(YEAR_RE.search(stem))


def version_kind(grp: str) -> str:
    return "episode" if grp.startswith("ep:") else "movie"


def version_order(prefer: str):
    """同一組不同版本的排序：第一個是建議保留的。解析度照偏好 → 檔名編號格式完整的 → 檔案小的 → 早上傳的。"""
    return lambda res, complete, size, mtime, fid: (*res_rank(res, prefer), 0 if complete else 1, size or 0,
                                                     mtime or 0, fid)


def exact_order(local: Optional[str], name: str, mtime: Optional[int], fid: int) -> tuple:
    """完全相同的排序：本機有 strm 的 → 檔名編號格式完整的 → 早上傳的。"""
    return (0 if local else 1, 0 if name_complete(name) else 1, mtime or 0, fid)
DELETE_BATCH = 100  # 一次請求送幾個檔案進回收站

# 檔名裡看得出來的畫質（沒有媒體資訊時用）
RES_RE = re.compile(r"(?<![0-9])(2160|1080|720|576|480)[pi](?![0-9])|(?<![a-z])(4k|uhd)(?![a-z])", re.I)
DV_RE = re.compile(r"(?<![a-z])(dv|dovi|dolby[ ._-]?vision)(?![a-z])", re.I)
HDR_RE = re.compile(r"(?<![a-z])(hdr10\+|hdr10|hdr)(?![a-z0-9])", re.I)
CODEC_RE = {"HEVC": re.compile(r"x265|h\.?265|hevc", re.I), "H264": re.compile(r"x264|h\.?264|avc", re.I),
            "AV1": re.compile(r"(?<![a-z])av1(?![a-z0-9])", re.I)}
# 分段檔：CD1、Disc 2、Part 3……同一部片的不同段，不是重複
PART_RE = re.compile(r"(?:^|[ ._\-\[(])(?:cd|dvd|disc|disk|part|pt)[ ._-]?\d{1,2}(?=[ ._\-\])]|$)", re.I)
# 一個檔案好幾集：E01E02、E01-E02、E01-02
MULTI_EP_RE = re.compile(r"[Ee]\d{1,4}[ ._]?[-~]?[ ._]?[Ee]\d{1,4}|[Ee]\d{1,3}-\d{1,3}(?![0-9p])")
# 版本名不同的是不同剪輯，分開算
EDITION_RE = re.compile(
    r"director'?s[ ._-]?cut|extended|unrated|uncut|theatrical|imax|remaster(?:ed)?|criterion|"
    r"导演剪辑|導演剪輯|加长|加長|未删减|未刪減|完整版|特别版|特別版", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", simplified(text or "").lower())


def quality(info: Optional[dict], name: str) -> dict:
    """一個版本的畫質：先看媒體資訊，沒有的部分從檔名猜。res 是解析度等級（2160、1080……），寬銀幕看寬度。"""
    q: dict = {"res": None, "hdr": None, "codec": None, "audio": None, "audios": 0, "subtitles": 0,
               "from": "mediainfo" if info else "filename"}
    if info:
        streams = info["source"].get("MediaStreams") or []
        video = next((st for st in streams if st.get("Type") == "Video"), None)
        if video:
            w, h = int(video.get("Width") or 0), int(video.get("Height") or 0)
            q["res"] = 2160 if w >= 3200 else 1080 if w >= 1800 else 720 if w >= 1200 else (h or None)
            ext, vr = video.get("ExtendedVideoType"), video.get("VideoRange")
            q["hdr"] = ext if ext and ext != "None" else (vr if vr and vr != "SDR" else None)
            q["codec"] = (video.get("Codec") or "").upper() or None
        audios = [st for st in streams if st.get("Type") == "Audio"]
        q["audios"] = len(audios)
        if audios:
            a = audios[0]
            channels = a.get("ChannelLayout") or (f"{a['Channels']}ch" if a.get("Channels") else "")
            q["audio"] = " ".join(x for x in ((a.get("Codec") or "").upper(), channels) if x) or None
        q["subtitles"] = sum(1 for st in streams if st.get("Type") == "Subtitle")
    if not q["res"]:
        m = RES_RE.search(name)
        if m:
            q["res"] = 2160 if m.group(2) else int(m.group(1))
    if not q["hdr"]:
        hdr = HDR_RE.search(name)
        q["hdr"] = "DolbyVision" if DV_RE.search(name) else (hdr.group(1).upper() if hdr else None)
    if not q["codec"]:
        q["codec"] = next((c for c, rx in CODEC_RE.items() if rx.search(name)), None)
    return q


@dataclass
class DupeJob:
    """找重複或刪重複的進度；網頁每幾秒查一次。"""

    kind: str = ""  # scan = 找重複、delete = 刪重複
    running: bool = False
    started: float = 0.0
    finished: float = 0.0
    listed: int = 0  # 找重複：看過幾個 115 檔案
    total: int = 0  # 刪重複：要刪幾個
    done: int = 0  # 刪重複：已經刪了幾個
    freed: int = 0  # 刪重複：省下幾個位元組
    current: str = ""
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class DupeFinder:
    def __init__(self, db: Database, p115: P115Service, strm_sync, scanner):
        self.db = db
        self.p115 = p115
        self.strm_sync = strm_sync
        self.scanner = scanner
        self.job = DupeJob()
        self._lock = threading.Lock()
        if db.get_meta(VERSIONS_RULE_KEY) != VERSIONS_RULE:
            # 第 1 版會把刮削時沒認出集號（nfo 寫 -1）的不同集算成同一集，清掉免得照著刪錯
            if db.one("SELECT 1 FROM dup_versions LIMIT 1"):
                db.execute("DELETE FROM dup_versions")
                db.set_meta(VERSIONS_OUTDATED_KEY, "1")
                log.info("不同版本的判斷規則更新了，清掉舊的結果，請重新找重複")
            db.set_meta(VERSIONS_RULE_KEY, VERSIONS_RULE)
        if db.get_meta(PREFER_APPLIED_KEY) != self.prefer() or db.get_meta(SUGGEST_RULE_KEY) != SUGGEST_RULE:
            self._resuggest()  # 舊結果是照舊規則（或別的偏好）算的

    # ---------------- 範圍與狀態 ----------------

    def default_roots(self) -> List[str]:
        """同步任務的 115 目錄；互相包含的只留外層，免得同一個檔案列兩次。"""
        roots = sorted({"/" + t.remote.strip("/") for t in self.strm_sync.tasks}, key=len)
        out: List[str] = []
        for root in roots:
            if not any(root == o or root.startswith(o.rstrip("/") + "/") for o in out):
                out.append(root)
        return out

    def summary(self) -> dict:
        meta = json.loads(self.db.get_meta(SCAN_META_KEY) or "{}")
        stats = self.db.one(
            "SELECT COUNT(*) AS files, COUNT(DISTINCT sha1 || ':' || size) AS groups, "
            "COALESCE(SUM(CASE WHEN keep THEN 0 ELSE size END), 0) AS reclaimable FROM dup_files"
        )
        return {
            "job": self.job.as_dict(),
            "scanned_at": meta.get("at"),
            "roots": meta.get("roots") or [],
            "scanned_files": meta.get("files", 0),
            "groups": stats["groups"],
            "files": stats["files"],
            "reclaimable": stats["reclaimable"],  # 照建議刪掉可以省下的位元組
            "versions": dict(self.db.one(  # 不同版本：幾組、幾個檔案、照建議刪可以省多少
                "SELECT COUNT(*) AS files, COUNT(DISTINCT grp) AS groups, "
                "COALESCE(SUM(CASE WHEN keep THEN 0 ELSE size END), 0) AS reclaimable FROM dup_versions")),
            "versions_outdated": bool(self.db.get_meta(VERSIONS_OUTDATED_KEY)),  # 規則更新後清掉了，要重新找
            "prefer": self.prefer(),  # 不同版本建議保留哪種解析度
            "default_roots": self.default_roots(),
        }

    def busy(self) -> bool:
        return self._lock.locked()

    # ---------------- 建議保留哪個版本 ----------------

    def prefer(self) -> str:
        value = self.db.get_meta(PREFER_KEY)
        return value if value in PREFERS else DEFAULT_PREFER

    def set_prefer(self, prefer: str) -> None:
        """換偏好，已找到的不同版本當場重算建議；找重複或刪除進行中不能換。"""
        if prefer not in PREFERS:
            raise ValueError("不認得的偏好")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("正在找重複或刪除，等它完成再換")
        try:
            self.db.set_meta(PREFER_KEY, prefer)
            self._resuggest()
        finally:
            self._lock.release()

    def _resuggest(self) -> None:
        """照現在的偏好和規則重新挑每一組建議保留的（畫質、檔名在找重複時已經存下來，不用再問 115）。"""
        prefer = self.prefer()
        order = version_order(prefer)
        groups: Dict[str, List[tuple]] = {}
        for r in self.db.query("SELECT file_id, grp, name, size, mtime, quality FROM dup_versions"):
            try:
                res = (json.loads(r["quality"] or "{}") or {}).get("res")
            except ValueError:
                res = None
            key = order(res, name_complete(r["name"], version_kind(r["grp"])), r["size"], r["mtime"], r["file_id"])
            groups.setdefault(r["grp"], []).append((key, r["file_id"]))
        exact: Dict[tuple, List[tuple]] = {}
        for r in self.db.query("SELECT file_id, sha1, size, name, local, mtime FROM dup_files"):
            exact.setdefault((r["sha1"], r["size"]), []).append(
                (exact_order(r["local"], r["name"], r["mtime"], r["file_id"]), r["file_id"]))
        pick = lambda gs: [(int(i == 0), fid) for members in gs for i, (_, fid) in enumerate(sorted(members))]  # noqa: E731
        with self.db.lock:
            self.db.conn.executemany("UPDATE dup_versions SET keep=? WHERE file_id=?", pick(groups.values()))
            self.db.conn.executemany("UPDATE dup_files SET keep=? WHERE file_id=?", pick(exact.values()))
            self.db.conn.commit()
        self.db.set_meta(PREFER_APPLIED_KEY, prefer)
        self.db.set_meta(SUGGEST_RULE_KEY, SUGGEST_RULE)

    # ---------------- 找重複 ----------------

    def scan_in_background(self, roots: Optional[List[str]] = None) -> bool:
        if self._lock.locked():
            return False
        self.job = DupeJob(kind="scan", running=True, started=time.time())
        threading.Thread(target=self.scan, args=(roots,), daemon=True).start()
        return True

    def scan(self, roots: Optional[List[str]] = None) -> DupeJob:
        """列出範圍內的影片，把 SHA1 和大小都一樣的分成一組，整批換掉上次的結果。"""
        if not self._lock.acquire(blocking=False):
            return self.job
        job = self.job if self.job.running and self.job.kind == "scan" else DupeJob(kind="scan", running=True, started=time.time())
        self.job = job
        try:
            roots = ["/" + r.strip("/") for r in roots or [] if str(r).strip()] or self.default_roots()
            if not roots:
                raise P115Error("還沒有同步任務，請選一個 115 目錄")
            files: Dict[int, dict] = {}
            without_sha1 = 0
            for root in roots:
                self.p115.breaker.check()
                job.current = root
                cid = self.p115.dir_id(root)
                for info in self.p115.iter_changed_files(cid, 0):  # since = 0：全部
                    job.listed += 1
                    if Path(info["name"]).suffix.lower() not in VIDEO_EXTS:
                        continue
                    if not info.get("sha1"):
                        without_sha1 += 1
                        continue
                    files[info["id"]] = info
            if without_sha1 and not files:
                raise P115Error("115 回傳的檔案清單沒有 SHA1，沒辦法比對")
            groups: Dict[Tuple[str, int], List[dict]] = {}
            for info in files.values():
                groups.setdefault((info["sha1"], info["size"]), []).append(info)
            rows = []
            folders: Dict[int, Optional[str]] = {}
            for members in (g for g in groups.values() if len(g) >= 2):
                located = []
                for info in members:
                    job.current = info["name"]
                    path, local = self._locate(info, folders)
                    located.append((info, path, local))
                # 建議保留：本機有 strm 的優先，其次檔名編號格式完整的，再來是最早上傳的
                located.sort(key=lambda m: exact_order(m[2], m[0]["name"], m[0]["mtime"], m[0]["id"]))
                for i, (info, path, local) in enumerate(located):
                    rows.append((info["id"], info["sha1"], info["size"], info["name"], info["pickcode"],
                                 info["parent_id"], path, local, info["mtime"], int(i == 0)))
            job.current = "比對同一部片的不同版本"
            versions = self._find_versions(files)
            with self.db.lock:
                self.db.conn.execute("DELETE FROM dup_files")
                self.db.conn.executemany(
                    "INSERT INTO dup_files(file_id, sha1, size, name, pickcode, parent_id, path, local, mtime, keep) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)", rows,
                )
                self.db.conn.execute("DELETE FROM dup_versions")
                self.db.conn.executemany(
                    "INSERT INTO dup_versions(file_id, grp, title, item_id, name, path, local, size, mtime, sha1, quality, keep) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", versions,
                )
                self.db.conn.commit()
            self.db.set_meta(SCAN_META_KEY, json.dumps({"at": int(time.time()), "roots": roots, "files": len(files)},
                                                       ensure_ascii=False))
            self.db.set_meta(VERSIONS_OUTDATED_KEY, "")
            self.db.set_meta(PREFER_APPLIED_KEY, self.prefer())
            self.db.set_meta(SUGGEST_RULE_KEY, SUGGEST_RULE)
            log.info("找重複：%s 看了 %s 支影片，完全相同的 %s 支、不同版本的 %s 支",
                     "、".join(roots), len(files), len(rows), len(versions))
        except P115Error as exc:
            job.errors.append(str(exc))
            log.warning("找重複失敗：%s", exc)
        except Exception as exc:  # 不能讓背景執行緒默默死掉，網頁要看得到原因
            job.errors.append(f"{type(exc).__name__}: {exc}")
            log.exception("找重複時發生未預期的錯誤")
        finally:
            job.running = False
            job.current = ""
            job.finished = time.time()
            self._lock.release()
        return job

    def _find_versions(self, files: Dict[int, dict]) -> List[tuple]:
        """同一部電影或同一集、檔案卻不同的：只看同步任務裡有 strm 的（媒體庫認得出是哪一部）。"""
        local_of: Dict[str, Tuple[int, str]] = {}  # 本機 strm → (115 檔案 id, 115 路徑)
        for task in self.strm_sync.tasks:
            root = Path(task.local).expanduser()
            remote = "/" + task.remote.strip("/")
            for r in self.db.query("SELECT file_id, path FROM p115_index WHERE task=? AND is_dir=0",
                                   (f"{task.remote}\n{task.local}",)):
                info = files.get(r["file_id"])
                if info:
                    local_of[str(root / r["path"])] = (
                        r["file_id"], posixpath.join(remote, posixpath.dirname(r["path"]), info["name"]))
        if not local_of:
            return []
        groups: Dict[str, List[tuple]] = {}
        titles: Dict[str, str] = {}
        for it in self.db.query(
            "SELECT i.id, i.type, i.path, i.name, i.year, i.provider_ids, i.series_id, i.parent_index_number, "
            "i.index_number, s.name AS series FROM items i LEFT JOIN items s ON s.id=i.series_id "
            "WHERE i.type IN ('Movie','Episode') AND i.is_strm=1"
        ):
            hit = local_of.get(it["path"])
            if not hit:
                continue
            info = files[hit[0]]
            name = info["name"]
            if PART_RE.search(name):
                continue
            if it["type"] == "Episode":
                ep, season = it["index_number"], it["parent_index_number"] or 0
                # 不知道第幾集的（沒寫，或刮削時沒認出來寫成 -1）不能比，否則整季都會變成「同一集」
                if ep is None or ep < 0 or season < 0 or not it["series_id"] or MULTI_EP_RE.search(name):
                    continue
                # 115 上的檔名看得出集號、卻和媒體庫對不上：寧可不列，免得刪錯
                name_season, name_ep = parse_episode(posixpath.splitext(name)[0])
                if (name_ep is not None and name_ep != ep) or (name_season is not None and name_season != season):
                    continue
                key = f"ep:{it['series_id']}:{season}:{ep}"
                title = f"{it['series'] or '?'} S{season:02d}E{it['index_number']:02d}"
            else:
                try:
                    tmdb = (json.loads(it["provider_ids"] or "{}") or {}).get("Tmdb")
                except ValueError:
                    tmdb = None
                if tmdb:
                    key = f"movie:tmdb:{tmdb}"
                elif it["name"] and it["year"]:
                    key = f"movie:name:{_norm(it['name'])}|{it['year']}"
                else:
                    continue
                title = f"{it['name']} ({it['year']})" if it["year"] else it["name"]
            edition = EDITION_RE.search(name)
            if edition:
                key += "|" + _norm(edition.group())
                title += f"（{edition.group()}）"
            titles.setdefault(key, title)
            groups.setdefault(key, []).append((it, info, hit[1]))
        store = MediaInfoStore(self.db)
        order = version_order(self.prefer())
        rows: List[tuple] = []
        for key, members in groups.items():
            if len(members) < 2 or len({(m[1]["sha1"], m[1]["size"]) for m in members}) < 2:
                continue  # 只有一份，或全都一模一樣（那是「完全相同」）
            scored = [(it, info, path, quality(store.get(it["path"]), info["name"])) for it, info, path in members]
            # 建議保留：照偏好的解析度（預設 1080P 優先，沒有再 4K），一樣時檔名編號格式完整的，再來檔案小的
            kind = version_kind(key)
            scored.sort(key=lambda m: order(m[3]["res"], name_complete(m[1]["name"], kind), m[1]["size"],
                                            m[1]["mtime"], m[1]["id"]))
            for i, (it, info, path, q) in enumerate(scored):
                rows.append((info["id"], key, titles[key], it["id"], info["name"], path, it["path"], info["size"],
                             info["mtime"], info["sha1"], json.dumps(q, ensure_ascii=False), int(i == 0)))
        return rows

    def _locate(self, info: dict, folders: Dict[int, Optional[str]]) -> Tuple[str, Optional[str]]:
        """115 上的完整路徑，以及本機的 strm（在同步任務裡、而且檔案還在時）。"""
        row = self.db.one("SELECT task, path FROM p115_index WHERE file_id=? AND is_dir=0", (info["id"],))
        if row:
            remote, _, local_root = row["task"].partition("\n")
            path = posixpath.join("/" + remote.strip("/"), posixpath.dirname(row["path"]), info["name"])
            local = Path(local_root).expanduser() / row["path"]
            return path, str(local) if local.is_file() else None
        # 不在同步任務裡：向 115 查資料夾路徑，每個資料夾只查一次
        parent = info["parent_id"]
        if parent not in folders:
            try:
                self.p115.breaker.check()
                folders[parent] = "/" + "/".join(name for _, name in self.p115.dir_ancestors(parent))
            except P115Error as exc:
                log.warning("查不到 115 資料夾 %s 的路徑：%s", parent, exc)
                folders[parent] = None
            delay = getattr(self.strm_sync.cfg, "request_delay", 0) or 0
            if delay:
                time.sleep(delay)
        base = folders[parent]
        return (posixpath.join(base, info["name"]) if base else info["name"]), None

    # ---------------- 清單 ----------------

    def groups(self, query: str = "", offset: int = 0, limit: int = 20, kind: str = "exact") -> dict:
        """一組一組列出來，可以省最多空間的在前面；query 比對檔名和路徑（不同版本也比對片名）。"""
        if kind == "versions":
            return self._version_groups(query, offset, limit)
        where, params = "", []
        if query.strip():
            where = "WHERE sha1 IN (SELECT sha1 FROM dup_files WHERE name LIKE ? OR path LIKE ?)"
            params = [f"%{query.strip()}%"] * 2
        total = self.db.one(f"SELECT COUNT(*) AS c FROM (SELECT 1 FROM dup_files {where} GROUP BY sha1, size)", params)["c"]
        keys = self.db.query(
            f"SELECT sha1, size, COUNT(*) AS n FROM dup_files {where} GROUP BY sha1, size "
            "ORDER BY size * (COUNT(*) - 1) DESC, sha1 LIMIT ? OFFSET ?", (*params, limit, offset),
        )
        items = []
        for k in keys:
            members = self.db.query(
                "SELECT * FROM dup_files WHERE sha1=? AND size=? ORDER BY keep DESC, mtime, file_id", (k["sha1"], k["size"])
            )
            items.append({"sha1": k["sha1"], "size": k["size"], "count": k["n"], "members": [
                {**self._member(m), "complete": name_complete(m["name"])} for m in members]})
        return {"items": items, "total": total}

    def _version_groups(self, query: str, offset: int, limit: int) -> dict:
        where, params = "", []
        if query.strip():
            where = "WHERE grp IN (SELECT grp FROM dup_versions WHERE title LIKE ? OR name LIKE ? OR path LIKE ?)"
            params = [f"%{query.strip()}%"] * 3
        total = self.db.one(f"SELECT COUNT(*) AS c FROM (SELECT 1 FROM dup_versions {where} GROUP BY grp)", params)["c"]
        keys = self.db.query(
            f"SELECT grp, MIN(title) AS title, COUNT(*) AS n, SUM(CASE WHEN keep THEN 0 ELSE size END) AS saving "
            f"FROM dup_versions {where} "
            "GROUP BY grp ORDER BY saving DESC, grp LIMIT ? OFFSET ?", (*params, limit, offset),
        )
        items = []
        for k in keys:
            members = self.db.query("SELECT * FROM dup_versions WHERE grp=? ORDER BY keep DESC, size DESC, file_id", (k["grp"],))
            kind = version_kind(k["grp"])
            items.append({"grp": k["grp"], "title": k["title"], "count": k["n"], "kind": kind, "members": [
                {**self._member(m), "size": m["size"], "quality": json.loads(m["quality"] or "{}"),
                 "complete": name_complete(m["name"], kind)} for m in members]})
        return {"items": items, "total": total}

    def _member(self, m) -> dict:
        item = self.db.one("SELECT id, type, name, series_id FROM items WHERE path=?", (m["local"],)) if m["local"] else None
        series = self.db.one("SELECT name FROM items WHERE id=?", (item["series_id"],)) if item and item["series_id"] else None
        watched = self.db.one(
            "SELECT COUNT(*) AS c FROM user_data WHERE item_id=? AND (played=1 OR position_ticks>0 OR is_favorite=1)",
            (item["id"],),
        )["c"] if item else 0
        return {
            "file_id": m["file_id"], "name": m["name"], "path": m["path"], "mtime": m["mtime"], "keep": bool(m["keep"]),
            "local": m["local"], "item": item["name"] if item else None, "series": series["name"] if series else None,
            "watched": bool(watched),
        }

    def recent_deletions(self, limit: int = 50) -> List[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM dup_deleted ORDER BY at DESC, rowid DESC LIMIT ?", (limit,))]

    # ---------------- 刪重複 ----------------

    def plan(self, overrides: Dict[int, bool], sha1: Optional[str] = None, size: Optional[int] = None,
             kind: str = "exact", grp: Optional[str] = None, use_suggestions: Optional[bool] = None) -> List[dict]:
        """要刪哪些：overrides 逐個指定（file_id → 要不要刪），沒指定的照預設。

        use_suggestions 為真時，沒指定的照建議刪（不是建議保留的都刪），為假時不刪；
        沒給的話，完全相同的照建議、不同版本的不刪（內容不同，要使用者自己挑）。
        給了 sha1＋size（完全相同）或 grp（不同版本）時只看那一組。每一組至少要留一份，不然丟 ValueError。
        """
        if kind == "versions":
            if grp:
                rows = self.db.query("SELECT * FROM dup_versions WHERE grp=? ORDER BY file_id", (grp,))
            else:
                rows = self.db.query("SELECT * FROM dup_versions ORDER BY grp, file_id")

            def key(r):
                return r["grp"]

            def default(r):
                return bool(use_suggestions) and not r["keep"]

            def label(r):
                return r["title"]
        else:
            if sha1:
                rows = self.db.query("SELECT * FROM dup_files WHERE sha1=? AND size=? ORDER BY file_id", (sha1, int(size or 0)))
            else:
                rows = self.db.query("SELECT * FROM dup_files ORDER BY sha1, size, file_id")

            def key(r):
                return (r["sha1"], r["size"])

            def default(r):
                return (use_suggestions is None or use_suggestions) and not r["keep"]

            def label(r):
                return r["name"]
        groups: Dict[object, List] = {}
        for r in rows:
            groups.setdefault(key(r), []).append(r)
        chosen: List[dict] = []
        for members in groups.values():
            picked = [m for m in members if overrides.get(m["file_id"], default(m))]
            if len(picked) == len(members):
                raise ValueError(f"「{label(members[0])}」這一組每一份都勾了，至少要留一份")
            chosen += [{**dict(m), "kind": kind} for m in picked]
        return chosen

    def delete_in_background(self, plan: List[dict]) -> bool:
        if not plan or self._lock.locked():
            return False
        self.job = DupeJob(kind="delete", running=True, started=time.time(), total=len(plan))
        threading.Thread(target=self.delete, args=(plan,), daemon=True).start()
        return True

    def delete(self, plan: List[dict]) -> DupeJob:
        """把 plan 裡的檔案送進 115 回收站，一次一批；每批成功後處理本機（觀看紀錄、strm、清單）。"""
        if not self._lock.acquire(blocking=False):
            return self.job
        job = self.job if self.job.running and self.job.kind == "delete" else DupeJob(
            kind="delete", running=True, started=time.time(), total=len(plan))
        self.job = job
        planned = {r["file_id"] for r in plan}
        removed: List[str] = []
        try:
            for start in range(0, len(plan), DELETE_BATCH):
                batch = plan[start: start + DELETE_BATCH]
                job.current = batch[0]["name"]
                self.p115.delete_files([r["file_id"] for r in batch])
                removed += self._after_delete(batch, planned)
                job.done += len(batch)
                job.freed += sum(r["size"] for r in batch)
                delay = getattr(self.strm_sync.cfg, "request_delay", 0) or 0
                if delay and start + DELETE_BATCH < len(plan):
                    time.sleep(delay)
            log.info("刪重複：%s 個檔案送進 115 回收站，省下 %.1f GB", job.done, job.freed / 1024 ** 3)
        except P115Error as exc:
            job.errors.append(f"刪到第 {job.done + 1} 個時失敗：{exc}")
            log.warning("刪重複失敗：%s", exc)
        except Exception as exc:
            job.errors.append(f"{type(exc).__name__}: {exc}")
            log.exception("刪重複時發生未預期的錯誤")
        finally:
            job.current = "重新掃描媒體庫" if removed else ""
            try:
                if removed:
                    self.scanner.scan_paths(removed)  # 刪掉的 strm 從媒體庫移除
            finally:
                job.running = False
                job.current = ""
                job.finished = time.time()
                self._lock.release()
        return job

    def _after_delete(self, batch: List[dict], planned: Set[int]) -> List[str]:
        """115 上已經刪了：觀看紀錄轉到保留的那份、刪本機 strm、記下刪了什麼、從清單拿掉。"""
        for r in batch:
            if r["local"]:
                if r.get("kind") == "versions":
                    copies = self.db.query(
                        "SELECT file_id, local FROM dup_versions WHERE grp=? AND local IS NOT NULL ORDER BY keep DESC, size DESC",
                        (r["grp"],))
                else:
                    copies = self.db.query(
                        "SELECT file_id, local FROM dup_files WHERE sha1=? AND size=? AND local IS NOT NULL "
                        "ORDER BY keep DESC, mtime", (r["sha1"], r["size"]))
                keeper = next((c for c in copies if c["file_id"] not in planned), None)  # 同一組裡不刪的那份
                if keeper:
                    self._move_user_data(r["local"], keeper["local"])
        removed = self.strm_sync.remove_local([r["file_id"] for r in batch])
        now = int(time.time())
        with self.db.lock:
            c = self.db.conn
            c.executemany("INSERT INTO dup_deleted(file_id, sha1, size, name, path, at) VALUES(?,?,?,?,?,?)",
                          [(r["file_id"], r.get("sha1") or "", r["size"], r["name"], r["path"], now) for r in batch])
            # 兩種清單都拿掉（同一個檔案可能兩邊都有），只剩一份的組不算重複了
            c.executemany("DELETE FROM dup_files WHERE file_id=?", [(r["file_id"],) for r in batch])
            c.executemany("DELETE FROM dup_versions WHERE file_id=?", [(r["file_id"],) for r in batch])
            c.execute("DELETE FROM dup_files WHERE sha1 || ':' || size IN "
                      "(SELECT sha1 || ':' || size FROM dup_files GROUP BY sha1, size HAVING COUNT(*) < 2)")
            c.execute("DELETE FROM dup_versions WHERE grp IN (SELECT grp FROM dup_versions GROUP BY grp HAVING COUNT(*) < 2)")
            c.commit()
        return removed

    def _move_user_data(self, src_path: str, dst_path: str) -> None:
        """刪掉的那份上的觀看紀錄（看過、進度、收藏）併到保留的那份，每個使用者分開算。"""
        src = self.db.one("SELECT id FROM items WHERE path=?", (src_path,))
        dst = self.db.one("SELECT id FROM items WHERE path=?", (dst_path,))
        if not src or not dst or src["id"] == dst["id"]:
            return
        for r in self.db.query("SELECT * FROM user_data WHERE item_id=?", (src["id"],)):
            mine = self.db.one("SELECT * FROM user_data WHERE user_id=? AND item_id=?", (r["user_id"], dst["id"]))
            if not mine:
                self.db.execute(
                    "INSERT INTO user_data(user_id, item_id, played, play_count, position_ticks, is_favorite, last_played) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (r["user_id"], dst["id"], r["played"], r["play_count"], r["position_ticks"], r["is_favorite"], r["last_played"]),
                )
                continue
            newer = (r["last_played"] or "") > (mine["last_played"] or "")  # 續播點用最近看的那一份
            self.db.execute(
                "UPDATE user_data SET played=?, play_count=?, position_ticks=?, is_favorite=?, last_played=? "
                "WHERE user_id=? AND item_id=?",
                (max(r["played"] or 0, mine["played"] or 0), (r["play_count"] or 0) + (mine["play_count"] or 0),
                 r["position_ticks"] if newer else mine["position_ticks"], max(r["is_favorite"] or 0, mine["is_favorite"] or 0),
                 max(r["last_played"] or "", mine["last_played"] or "") or None, r["user_id"], dst["id"]),
            )

