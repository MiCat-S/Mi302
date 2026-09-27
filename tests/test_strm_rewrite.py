"""strm 裡的伺服器網址改了（例如改成反向代理的網址）：只改本機檔案，不連 115，也不必重新同步。"""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.mediainfo import MediaInfoStore
from embyserver.strm_sync import FULL

from test_incremental import Fake115, make

PC = "a" * 17


def test_rewrite_changes_only_mi302_strm_without_touching_115(tmp_path: Path):
    fake = Fake115()
    sync = make(tmp_path, fake)
    sync.run(FULL)
    media = tmp_path / "media"
    movie = media / "電影" / "Old Movie (2001).strm"
    assert movie.read_text() == f"http://127.0.0.1:8096/d/{PC}.mkv"
    MediaInfoStore(sync.p115.db).put(str(movie), {"source": {}}, 0, "ffprobe")
    # 別的工具產生的 strm、本機路徑：不動
    alist = media / "電影" / "alist.strm"
    alist.write_text("http://alist.local:5244/d/電影/a.mkv")
    local = media / "電影" / "local.strm"
    local.write_text("/mnt/cloud/a.mkv")

    fake.calls.clear()
    sync.cfg.base_url = "https://emby.example.com/mi302"  # 反向代理的網址，帶子路徑
    assert sync.follow_format() is True
    for _ in range(100):
        if not sync.rewrite_result.running:
            break
        time.sleep(0.02)
    r = sync.rewrite_result
    assert (r.rewritten, r.unchanged, r.skipped, r.errors) == (2, 0, 2, [])
    assert r.base_url == "https://emby.example.com/mi302"
    assert movie.read_text() == f"https://emby.example.com/mi302/d/{PC}.mkv"
    assert alist.read_text() == "http://alist.local:5244/d/電影/a.mkv" and local.read_text() == "/mnt/cloud/a.mkv"
    assert fake.calls == []  # 一個 115 請求都沒有
    assert MediaInfoStore(sync.p115.db).get(str(movie)) is not None  # pickcode 沒變，媒體資訊照用
    assert sync.follow_format() is False  # 設定沒再變就不重做

    # 附上原檔名：從 strm 檔名還原；再關掉就拿掉
    sync.cfg.include_name = True
    sync.follow_format()
    sync.rewrite_strm()
    assert movie.read_text() == f"https://emby.example.com/mi302/d/{PC}.mkv?/Old%20Movie%20%282001%29.mkv"
    sync.cfg.include_name = False
    sync.rewrite_strm()
    assert movie.read_text() == f"https://emby.example.com/mi302/d/{PC}.mkv"
    assert sync.rewrite_strm().unchanged == 2

    # 之後同步產生的 strm 也用新網址，已經改好的不會再被當成要更新
    r = sync.run(FULL)
    assert r.strm_unchanged == 2 and r.strm_created == 0


def test_settings_save_rewrites_in_background(tmp_path: Path):
    media = tmp_path / "media"
    old = media / "劇集" / "Dark" / "Dark.S01E01.strm"
    old.parent.mkdir(parents=True)
    old.write_text(f"http://192.168.1.10:8096/d/{PC}.mkv")
    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")},
        "users": [{"name": "admin", "password": "pw", "admin": True}],
        "libraries": [{"name": "劇集", "type": "tvshows", "paths": [str(media)]}],
        "p115": {"strm": {"tasks": [{"remote": "/影視", "local": str(media)}]}},
    }), scan_on_start=False)
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}

    r = c.put("/web/api/settings", json={"server": {"name": "家裡"}}, headers=h).json()
    assert r["notes"] == []  # 改別的設定不動 strm

    r = c.put("/web/api/settings", json={"p115": {"strm": {"base_url": "https://emby.example.com"}}}, headers=h).json()
    assert r["notes"] == ["現有的 strm 正在背景改成新的網址"]
    sync = app.state.strm_sync
    for _ in range(100):
        if not sync.rewrite_result.running:
            break
        time.sleep(0.02)
    assert old.read_text() == f"https://emby.example.com/d/{PC}.mkv"
    s = c.get("/p115/strm/status", headers=h).json()
    assert (s["base_url"], s["rewrite"]["rewritten"], s["rewrite"]["running"]) == ("https://emby.example.com", 1, False)

    # 手動按「把現有 strm 改成這個網址」
    old.write_text(f"http://10.0.0.5:8096/d/{PC}.mkv")
    assert c.post("/p115/strm/rewrite", headers=h).json()["started"] is True
    for _ in range(100):
        if not sync.rewrite_result.running:
            break
        time.sleep(0.02)
    assert old.read_text() == f"https://emby.example.com/d/{PC}.mkv"
