"""網頁上的「檢查更新」「更新到最新版」「重新啟動」。

程式資料夾是 git clone 下來的（install.sh 裝的都是），更新的做法和 install.sh update 一樣：
1. 檢查：git fetch 遠端的同一個分支，比較 HEAD 和 origin/分支，新的提交標題就是更新內容。
   啟動一分鐘後查一次，之後每 6 小時一次（server.update_check 關掉就只在網頁上按了才查）。
2. 更新：自己改過的程式檔備份成 local-changes-*.patch 再還原，切到 origin/分支；requirements.txt 有變就用
   目前這個 Python（.venv 裡的）安裝相依套件；再用新程式試著 import 一次。任何一步失敗就退回原本的版本、
   不重新啟動，網頁照常可用。
3. 重新啟動：請 uvicorn 停下（進行中的請求最多等 5 秒），__main__ 再用同一個指令 exec 自己。程序編號不變，
   systemd、launchd、背景執行都不用另外處理。

install.sh 本身管的東西（服務設定、ffprobe、Python 版本）網頁更新不會動，那些要在終端機執行 mi302 update。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from . import __version__
from .config import Config

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent  # 程式資料夾（git 倉庫的根目錄）
FIRST_CHECK = 60  # 啟動後多久第一次檢查（秒）
CHECK_EVERY = 6 * 3600
GIT_TIMEOUT = 90
PIP_TIMEOUT = 900
MAX_COMMITS = 50  # 更新內容最多列幾個提交
RESTART_DELAY = 1.0  # 先讓「要重新啟動了」的回應送出去


class UpdateError(Exception):
    pass


@dataclass
class UpdateJob:
    running: bool = False
    started: float = 0.0
    finished: float = 0.0
    step: str = ""
    error: str = ""
    message: str = ""
    old: str = ""
    new: str = ""
    patch: str = ""  # 自己改過的程式檔備份在哪
    restarting: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class CheckResult:
    checking: bool = False
    at: float = 0.0
    error: str = ""
    remote: str = ""
    behind: Optional[int] = None  # 落後幾個提交；沒權限 fetch、只能比對編號時是 None
    ahead: int = 0  # 本機有、遠端沒有的提交（更新時會捨棄）
    commits: List[dict] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return bool(self.behind) if self.behind is not None else bool(self.remote)

    def as_dict(self) -> dict:
        return {**asdict(self), "available": self.available}


def restart_argv() -> List[str]:
    """重新啟動時 exec 的指令：同一個 Python、python -m embyserver 加上原本的參數。"""
    return [sys.executable, "-m", "embyserver", *sys.argv[1:]]


def restart_env() -> dict:
    """PYTHONPATH 帶上程式資料夾，從別的工作目錄啟動的也找得到 embyserver。"""
    env = dict(os.environ)
    paths = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if str(ROOT) not in paths:
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT), *paths])
    return env


class Updater:
    def __init__(self, config: Config, root: Path = ROOT, python: str = sys.executable, import_check: str = "embyserver.app"):
        self.config = config
        self.root = Path(root)
        self.python = python
        self.import_check = import_check
        self.boot = uuid.uuid4().hex[:12]  # 每次啟動不同：網頁用它看出重新啟動完成了
        self.started = time.time()
        self.restart_cb: Optional[Callable[[], None]] = None  # __main__ 設定；沒設定（測試、別的方式啟動）就不能重新啟動
        self.check_result = CheckResult()
        self.job = UpdateJob()
        self._busy = threading.Lock()  # 檢查和更新不同時跑
        self._stop = threading.Event()
        self._notified = ""  # 日誌只在發現新的遠端版本時寫一次

    # ---------------- git ----------------

    def _run(self, cmd: List[str], timeout: float, env: Optional[dict] = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, cwd=self.root, capture_output=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise UpdateError(f"找不到 {cmd[0]} 指令")
        except subprocess.TimeoutExpired:
            raise UpdateError(f"{' '.join(cmd[:2])} 超過 {int(timeout)} 秒沒有完成")

    def _git_proc(self, *args: str, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        # 程式資料夾的擁有者可能不是執行 Mi302 的帳號，git 會拒絕（dubious ownership）；install.sh 也是這樣帶
        return self._run(["git", "-c", f"safe.directory={self.root}", "-c", "core.quotepath=false", "-C", str(self.root), *args],
                         timeout, env)

    def _git(self, *args: str, timeout: float = GIT_TIMEOUT) -> str:
        r = self._git_proc(*args, timeout=timeout)
        if r.returncode:
            why = (r.stderr or r.stdout).decode("utf-8", "replace").strip().splitlines()
            raise UpdateError(f"git {args[0]} 失敗：{why[-1] if why else r.returncode}")
        return r.stdout.decode("utf-8", "replace").strip()

    def is_git(self) -> bool:
        return (self.root / ".git").exists()

    def branch(self) -> str:
        r = self._git_proc("symbolic-ref", "--short", "-q", "HEAD")
        return r.stdout.decode().strip() if r.returncode == 0 and r.stdout.strip() else "main"

    def current(self) -> dict:
        """目前的版本：提交編號、時間、標題；不是 git 安裝的只有版本號。"""
        info = {"version": __version__, "commit": "", "date": 0, "subject": "", "branch": ""}
        if not self.is_git():
            return info
        try:
            sha, ts, subject = self._git("log", "-1", "--format=%H%x1f%ct%x1f%s").split("\x1f", 2)
            info.update(commit=sha, date=int(ts), subject=subject, branch=self.branch())
        except (UpdateError, ValueError):
            pass
        return info

    def cannot_update(self) -> str:
        """不能在網頁上更新的原因；可以的話是空字串。"""
        if not self.is_git():
            return "程式資料夾不是用 git 下載的，網頁上不能更新；請在終端機執行 mi302 update"
        if shutil.which("git") is None:
            return "這台機器沒有 git 指令，網頁上不能更新；請在終端機執行 mi302 update"
        venv = Path(sys.prefix)
        for path in (self.root, self.root / ".git", venv):
            if not os.access(path, os.W_OK):
                who = _whoami()
                return f"Mi302 以 {who} 身分執行，沒有權限改 {path}；請在終端機執行 mi302 update"
        return ""

    # ---------------- 檢查 ----------------

    def _fetch(self, branch: str) -> None:
        self._git("fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")

    def check(self) -> dict:
        """向遠端查有沒有新版，結果記在 check_result。已經在檢查或更新時直接回傳目前的狀態。"""
        if not self._busy.acquire(blocking=False):
            return self.status()
        res = CheckResult(checking=True, at=self.check_result.at)
        self.check_result = res
        try:
            if not self.is_git():
                raise UpdateError("程式資料夾不是用 git 下載的，查不到新版本")
            branch = self.branch()
            if os.access(self.root / ".git", os.W_OK):
                self._fetch(branch)
                ref = f"origin/{branch}"
                res.remote = self._git("rev-parse", ref)
                res.behind = int(self._git("rev-list", "--count", f"HEAD..{ref}") or 0)
                res.ahead = int(self._git("rev-list", "--count", f"{ref}..HEAD") or 0)
                out = self._git("log", f"-{MAX_COMMITS}", "--format=%H%x1f%ct%x1f%s", f"HEAD..{ref}")
                res.commits = [dict(zip(("commit", "date", "subject"), line.split("\x1f", 2))) for line in out.splitlines() if line]
                for c in res.commits:
                    c["date"] = int(c["date"])
                if not res.behind:
                    res.remote = ""
            else:  # 沒有權限寫 .git：只比對遠端的提交編號
                line = self._git("ls-remote", "origin", f"refs/heads/{branch}")
                remote = line.split()[0] if line else ""
                res.remote = remote if remote and remote != self._git("rev-parse", "HEAD") else ""
            if res.available and res.remote != self._notified:
                self._notified = res.remote
                log.info("Mi302 有新版本%s，可以在網頁「設定」頁更新", f"（{res.behind} 個更新）" if res.behind else "")
        except UpdateError as exc:
            res.error = str(exc)
            log.info("檢查 Mi302 更新失敗：%s", exc)
        finally:
            res.checking = False
            res.at = time.time()
            self._busy.release()
        return self.status()

    def start(self) -> None:
        """啟動一分鐘後查一次，之後每 6 小時；設定關掉時不查（網頁上按「檢查更新」照樣可以）。"""

        def loop():
            wait = FIRST_CHECK
            while not self._stop.wait(wait):
                wait = CHECK_EVERY
                if self.config.server.update_check and self.is_git():
                    try:
                        self.check()
                    except Exception:  # 背景執行緒不能因為意外錯誤停掉
                        log.exception("檢查 Mi302 更新時發生錯誤")

        threading.Thread(target=loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # ---------------- 更新 ----------------

    def update_in_background(self) -> None:
        reason = self.cannot_update()
        if reason:
            raise UpdateError(reason)
        if not self._busy.acquire(blocking=False):
            raise UpdateError("正在檢查或更新，等一下再試")
        self.job = UpdateJob(running=True, started=time.time(), step="下載新版")
        threading.Thread(target=self._update, daemon=True).start()

    def _update(self) -> None:
        job = self.job
        try:
            branch = self.branch()
            job.old = self._git("rev-parse", "HEAD")
            self._fetch(branch)
            job.new = self._git("rev-parse", f"origin/{branch}")
            if job.new == job.old:
                job.message = "已經是最新版"
                self.check_result = CheckResult(at=time.time(), behind=0)
                return
            self._backup_local_changes(job)
            job.step = "切換到新版"
            self._git("checkout", "-q", "-B", branch, f"origin/{branch}")
            try:
                if self._git_proc("diff", "--quiet", job.old, job.new, "--", "requirements.txt").returncode:
                    job.step = "安裝相依套件"
                    self.install_requirements()
                job.step = "檢查新版能不能啟動"
                self._import_check()
            except UpdateError:
                job.step = "退回原本的版本"
                self._git("checkout", "-q", "-B", branch, job.old)
                raise
            log.info("Mi302 已更新：%s → %s", job.old[:7], job.new[:7])
            self.check_result = CheckResult(at=time.time(), behind=0)
            if self.restart_cb:
                job.step = "重新啟動"
                job.restarting = True
                self.restart()
            else:
                job.message = "已更新，重新啟動 Mi302 後生效"
        except UpdateError as exc:
            job.error = str(exc)
            log.warning("Mi302 更新失敗：%s", exc)
        except Exception as exc:  # 背景執行緒：記下來，不讓網頁一直顯示「更新中」
            job.error = f"{type(exc).__name__}: {exc}"
            log.exception("Mi302 更新時發生錯誤")
        finally:
            if not job.restarting:
                job.step = ""
            job.running = False
            job.finished = time.time()
            self._busy.release()

    def _backup_local_changes(self, job: UpdateJob) -> None:
        """跟 install.sh 一樣：自己改過的程式檔先存成 patch，再還原成原本的樣子。"""
        if self._git_proc("diff", "--quiet", "HEAD", "--").returncode == 0:
            return
        patch = self.root / time.strftime("local-changes-%Y%m%d-%H%M%S.patch")
        diff = self._git_proc("diff", "HEAD", "--")
        patch.write_bytes(diff.stdout)
        job.patch = str(patch)
        log.warning("程式資料夾裡有自己改過的檔案，已備份成 %s，然後還原成原本的版本", patch)
        self._git("reset", "-q", "--hard")

    def install_requirements(self) -> None:
        """用目前這個 Python 安裝 requirements.txt；install.sh 記下的 pip 鏡像照用。"""
        req = str(self.root / "requirements.txt")
        mirror = _env_value(self.root / ".env", "PIP_MIRROR")
        if self._run([self.python, "-m", "pip", "--version"], 60).returncode == 0:
            cmd = [self.python, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", req]
        elif shutil.which("uv"):  # uv 建的虛擬環境沒有 pip
            cmd = ["uv", "pip", "install", "-q", "--python", self.python, "-r", req]
        else:
            raise UpdateError("目前的 Python 沒有 pip，裝不了新的相依套件；請在終端機執行 mi302 update")
        if mirror:
            cmd += ["-i", mirror]
        r = self._run(cmd, PIP_TIMEOUT)
        if r.returncode:
            why = (r.stderr or r.stdout).decode("utf-8", "replace").strip().splitlines()
            raise UpdateError("安裝相依套件失敗：" + (why[-1] if why else str(r.returncode)))

    def _import_check(self) -> None:
        """在另一個程序裡 import 新版，語法錯誤、少了套件的話不重新啟動。"""
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in (str(self.root), os.environ.get("PYTHONPATH", "")) if p)}
        r = self._run([self.python, "-c", f"import {self.import_check}"], 120, env)
        if r.returncode:
            why = r.stderr.decode("utf-8", "replace").strip().splitlines()
            raise UpdateError("新版啟動不了，已退回原本的版本：" + (why[-1] if why else str(r.returncode)))

    # ---------------- 重新啟動 ----------------

    def restart(self) -> None:
        if not self.restart_cb:
            raise UpdateError("Mi302 不是用 python -m embyserver 啟動的，網頁上不能重新啟動")
        log.warning("網頁上要求重新啟動 Mi302")
        threading.Timer(RESTART_DELAY, self.restart_cb).start()

    # ---------------- 狀態 ----------------

    def status(self) -> dict:
        return {
            **self.current(), "boot": self.boot, "started": self.started, "git": self.is_git(),
            "can_update": not self.cannot_update(), "update_reason": self.cannot_update(),
            "can_restart": self.restart_cb is not None, "auto": bool(self.config.server.update_check),
            "check": self.check_result.as_dict(), "job": self.job.as_dict(),
        }


def _whoami() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return os.environ.get("USER") or os.environ.get("USERNAME") or "目前的帳號"


def _env_value(path: Path, key: str) -> str:
    """install.sh 產生的 .env（KEY="值"）裡的一個值；沒有是空字串。"""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"')
    except OSError:
        pass
    return ""
