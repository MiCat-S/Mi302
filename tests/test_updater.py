"""網頁上的檢查更新、更新、重新啟動：用本機的兩個 git 倉庫當「GitHub」和「程式資料夾」，不連網路。"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from embyserver import updater as updater_mod
from embyserver.app import create_app
from embyserver.config import Config, config_from_dict
from embyserver.updater import ROOT, Updater, restart_argv, restart_env

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def commit(repo: Path, files: dict, message: str) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path: Path, monkeypatch):
    """origin 是「GitHub」，app 是 clone 下來的程式資料夾；套件叫 fakeapp。"""
    monkeypatch.setattr(updater_mod, "RESTART_DELAY", 0)
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    commit(origin, {"fakeapp/__init__.py": "X = 1\n", "requirements.txt": "# 沒有相依套件\n"}, "第一版")
    app = tmp_path / "app"
    subprocess.run(["git", "clone", "-q", str(origin), str(app)], check=True)
    up = Updater(Config(), root=app, import_check="fakeapp")
    restarts = []
    up.restart_cb = lambda: restarts.append(time.time())
    installs = []
    monkeypatch.setattr(up, "install_requirements", lambda: installs.append(1))
    return origin, app, up, restarts, installs


def wait(cond, seconds: float = 20):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("等不到")


def run_update(up: Updater):
    up.update_in_background()
    wait(lambda: not up.job.running)
    return up.job


def test_check_lists_new_commits(repos):
    origin, app, up, _, _ = repos
    s = up.check()
    assert s["git"] and s["branch"] == "main" and s["subject"] == "第一版" and s["can_update"] and s["can_restart"]
    assert (s["check"]["available"], s["check"]["behind"], s["check"]["error"]) == (False, 0, "")
    commit(origin, {"fakeapp/a.py": "A = 1\n"}, "新功能：甲")
    commit(origin, {"fakeapp/b.py": "B = 1\n"}, "修正：乙")
    c = up.check()["check"]
    assert c["available"] and c["behind"] == 2 and [x["subject"] for x in c["commits"]] == ["修正：乙", "新功能：甲"]
    assert c["remote"] == git(origin, "rev-parse", "HEAD") and c["at"] > 0
    assert git(app, "rev-parse", "HEAD") != c["remote"]  # 只是查，沒有換版本


def test_update_switches_version_and_restarts(repos):
    origin, app, up, restarts, installs = repos
    old = git(app, "rev-parse", "HEAD")
    new = commit(origin, {"fakeapp/__init__.py": "X = 2\n"}, "第二版")
    job = run_update(up)
    assert (job.error, job.old, job.new, job.restarting) == ("", old, new, True)
    assert git(app, "rev-parse", "HEAD") == new and (app / "fakeapp/__init__.py").read_text() == "X = 2\n"
    wait(lambda: restarts)
    assert not installs  # requirements.txt 沒變：不裝相依套件
    assert up.check()["check"]["available"] is False


def test_requirements_change_installs_packages(repos):
    origin, app, up, restarts, installs = repos
    commit(origin, {"requirements.txt": "# 還是沒有\n"}, "相依套件")
    assert not run_update(up).error
    assert installs == [1]


def test_broken_new_version_rolls_back(repos):
    origin, app, up, restarts, installs = repos
    old = git(app, "rev-parse", "HEAD")
    commit(origin, {"fakeapp/__init__.py": "X = (\n"}, "壞掉的版本")
    job = run_update(up)
    assert "啟動不了" in job.error and not job.restarting
    assert git(app, "rev-parse", "HEAD") == old and (app / "fakeapp/__init__.py").read_text() == "X = 1\n"
    time.sleep(0.1)
    assert not restarts


def test_failed_install_rolls_back(repos, monkeypatch):
    origin, app, up, restarts, _ = repos
    old = git(app, "rev-parse", "HEAD")
    commit(origin, {"requirements.txt": "no-such-package\n"}, "要新套件")

    def fail():
        raise updater_mod.UpdateError("安裝相依套件失敗：找不到套件")

    monkeypatch.setattr(up, "install_requirements", fail)
    job = run_update(up)
    assert job.error.startswith("安裝相依套件失敗") and git(app, "rev-parse", "HEAD") == old and not restarts


def test_local_changes_are_backed_up(repos):
    origin, app, up, restarts, _ = repos
    (app / "fakeapp/__init__.py").write_text("X = 1  # 自己改的\n", encoding="utf-8")
    commit(origin, {"fakeapp/c.py": "C = 1\n"}, "第二版")
    job = run_update(up)
    assert not job.error and job.patch and "自己改的" in Path(job.patch).read_text(encoding="utf-8")
    assert (app / "fakeapp/__init__.py").read_text() == "X = 1\n" and (app / "fakeapp/c.py").exists()


def test_already_latest_does_not_restart(repos):
    origin, app, up, restarts, _ = repos
    job = run_update(up)
    assert (job.error, job.message, job.restarting) == ("", "已經是最新版", False)
    time.sleep(0.1)
    assert not restarts


def test_not_a_git_install(tmp_path: Path):
    up = Updater(Config(), root=tmp_path, import_check="fakeapp")
    assert "不是用 git" in up.cannot_update()
    s = up.check()
    assert not s["git"] and not s["can_update"] and not s["can_restart"] and "不是用 git" in s["check"]["error"]
    with pytest.raises(updater_mod.UpdateError):
        up.update_in_background()
    with pytest.raises(updater_mod.UpdateError):
        up.restart()


def test_restart_command_keeps_arguments(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/x/embyserver/__main__.py", "-c", "/etc/mi302/config.yaml"])
    assert restart_argv() == [sys.executable, "-m", "embyserver", "-c", "/etc/mi302/config.yaml"]
    monkeypatch.setenv("PYTHONPATH", "/other")
    assert restart_env()["PYTHONPATH"].split(os.pathsep) == [str(ROOT), "/other"]


def test_server_api(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(updater_mod, "RESTART_DELAY", 0)
    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")}, "users": [{"name": "admin", "password": "pw", "admin": True},
                                                                  {"name": "kid", "password": "pw"}],
    }), scan_on_start=False)
    app.state.updater = Updater(app.state.config, root=tmp_path / "plain")
    c = TestClient(app)

    def token(name):
        return {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": name, "Pw": "pw"}).json()["AccessToken"]}

    h = token("admin")
    assert c.get("/web/api/server").status_code == 401 and c.get("/web/api/server", headers=token("kid")).status_code == 403
    s = c.get("/web/api/server", headers=h).json()
    assert s["boot"] == app.state.updater.boot and s["busy"] == [] and s["auto"] is True and not s["can_restart"]
    assert c.post("/web/api/server/update", headers=h).status_code == 400  # 不是 git 安裝的
    r = c.post("/web/api/server/restart", headers=h)
    assert r.status_code == 400 and "python -m embyserver" in r.text
    done = threading.Event()
    app.state.updater.restart_cb = done.set
    assert c.post("/web/api/server/restart", headers=h).status_code == 200 and done.wait(5)
    app.state.scanner.scanning = True
    assert c.get("/web/api/server", headers=h).json()["busy"] == ["媒體庫掃描"]


def test_last_check_survives_restart(repos):
    """重新啟動後接著用上次檢查的結果；網頁上更新到查到的新版後，重新啟動就是最新版。"""
    from embyserver.db import Database

    origin, app, up, restarts, _ = repos
    db = Database(":memory:")
    up.db = db
    commit(origin, {"fakeapp/a.py": "A = 1\n"}, "新功能")
    up.check()
    again = Updater(Config(), root=app, import_check="fakeapp", db=db)
    c = again.status()["check"]
    assert c["available"] and c["behind"] == 1 and c["commits"][0]["subject"] == "新功能" and c["at"]
    assert not run_update(up).error  # 換成新版，重新啟動
    after = Updater(Config(), root=app, import_check="fakeapp", db=db).status()["check"]
    assert (after["available"], after["behind"], after["error"]) == (False, 0, "") and after["at"]
    assert Updater(Config(), root=app, import_check="fakeapp").status()["check"]["at"] == 0  # 沒有資料庫：沒有紀錄


def test_update_proxy_and_github_mirror(repos, monkeypatch):
    """連不上 GitHub 時：代理給 git 和安裝相依套件用，GitHub 加速網址接在 origin 的 GitHub 網址前面下載。"""
    origin, app, up, _, _ = repos
    s = up.config.server
    s.update_github_proxy = "https://ghfast.top/"
    git(app, "remote", "set-url", "origin", "git@github.com:MiCat-S/Mi302.git")
    assert up._remote() == "https://ghfast.top/https://github.com/MiCat-S/Mi302.git"
    git(app, "remote", "set-url", "origin", "https://github.com/MiCat-S/Mi302.git")
    assert up._remote() == "https://ghfast.top/https://github.com/MiCat-S/Mi302.git"
    git(app, "remote", "set-url", "origin", str(origin))
    with pytest.raises(updater_mod.UpdateError, match="只能用在從 GitHub"):
        up._remote()
    s.update_github_proxy = ""
    assert up._remote() == "origin"

    # 代理：git 每個指令都帶 http.proxy（本機的遠端用不到，照樣查得到新版）；安裝相依套件帶代理的環境變數
    s.update_proxy = "http://127.0.0.1:7890"
    calls = []
    real = up._run
    monkeypatch.setattr(up, "_run", lambda cmd, timeout, env=None: calls.append((cmd, env)) or real(cmd, timeout, env))
    commit(origin, {"fakeapp/a.py": "A = 1\n"}, "新功能")
    assert up.check()["check"]["behind"] == 1
    fetch = next(cmd for cmd, _ in calls if "fetch" in cmd)
    assert "http.proxy=http://127.0.0.1:7890" in fetch
    calls.clear()
    monkeypatch.setattr(up, "_run", lambda cmd, timeout, env=None: calls.append((cmd, env)) or subprocess.CompletedProcess(cmd, 0, b"", b""))
    Updater.install_requirements(up)
    env = calls[-1][1]
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://127.0.0.1:7890"


def test_unreachable_remote_suggests_a_proxy(repos, tmp_path: Path):
    origin, app, up, _, _ = repos
    git(app, "remote", "set-url", "origin", str(tmp_path / "gone"))
    assert "填代理或 GitHub 加速網址" in up.check()["check"]["error"]
    up.config.server.update_proxy = "http://127.0.0.1:7890"  # 已經設了就不再提示
    assert "填代理" not in up.check()["check"]["error"]
