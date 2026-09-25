"""從 115 產生 strm 的測試，115 webapi 以 httpx.MockTransport 模擬。"""

from pathlib import Path

import httpx

from embyserver.config import P115StrmConfig, StrmTask
from embyserver.db import Database
from embyserver.p115 import P115Service
from embyserver.strm_sync import StrmSync

PC_MOVIE = "aaaaaaaaaaaaaaaa1"
PC_NFO = "aaaaaaaaaaaaaaaa2"
PC_EP = "aaaaaaaaaaaaaaaa3"
PC_TRAILER = "aaaaaaaaaaaaaaaa4"

# 115 上的目錄樹：/影視(100) -> 電影(101) -> Inception (2010)(102)；劇集(103) -> Dark(104)
TREE = {
    100: [
        {"cid": 101, "n": "電影"},
        {"cid": 103, "n": "劇集"},
    ],
    101: [{"cid": 102, "n": "Inception (2010)"}],
    102: [
        {"fid": 1, "cid": 102, "n": "Inception (2010).mkv", "pc": PC_MOVIE, "s": 5_000_000_000},
        {"fid": 2, "cid": 102, "n": "movie.nfo", "pc": PC_NFO, "s": 5},
        {"fid": 5, "cid": 102, "n": "trailer.mp4", "pc": PC_TRAILER, "s": 1_000_000},
    ],
    103: [{"cid": 104, "n": "Dark"}],
    104: [{"fid": 3, "cid": 104, "n": "Dark.S01E01.mp4", "pc": PC_EP, "s": 900_000_000}],
}


def handler(request: httpx.Request) -> httpx.Response:
    assert "UID=1" in request.headers["cookie"]
    if request.url.path == "/files/getid":
        return httpx.Response(200, json={"state": True, "id": "100" if request.url.params["path"] == "/影視" else "0"})
    if request.url.path == "/files":
        cid = int(request.url.params["cid"])
        items = TREE[cid]
        return httpx.Response(200, json={"state": True, "count": len(items), "data": items, "path": [{"cid": 0}, {"cid": cid}]})
    return httpx.Response(404)


def make_sync(tmp_path: Path, **kw) -> StrmSync:
    svc = P115Service(Database(":memory:"), initial_cookies="UID=1", transport=httpx.MockTransport(handler))
    svc.download_url = lambda pc, ua="": f"https://cdn.115.test/{pc}"
    cfg = P115StrmConfig(
        tasks=[StrmTask(remote="/影視", local=str(tmp_path / "media"))], request_delay=0, **kw
    )
    sync = StrmSync(svc, cfg)
    sync._http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<nfo>")))
    return sync


def test_generate_strm_and_metadata(tmp_path: Path):
    sync = make_sync(tmp_path, base_url="http://nas:8096", min_size_mb=10)
    r = sync.run()
    media = tmp_path / "media"
    movie = media / "電影" / "Inception (2010)" / "Inception (2010).strm"
    assert movie.read_text() == f"http://nas:8096/p115/redirect?pickcode={PC_MOVIE}&file_name=Inception%20%282010%29.mkv"
    assert (media / "劇集" / "Dark" / "Dark.S01E01.strm").read_text().endswith(f"pickcode={PC_EP}&file_name=Dark.S01E01.mp4")
    assert (media / "電影" / "Inception (2010)" / "movie.nfo").read_bytes() == b"<nfo>"
    # 1MB 的預告片低於 min_size_mb，不產生
    assert not (media / "電影" / "Inception (2010)" / "trailer.strm").exists()
    assert r.strm_created == 2 and r.metadata_downloaded == 1 and not r.errors

    # 第二次同步：內容相同就不重寫
    r2 = sync.run()
    assert r2.strm_created == 0 and r2.strm_unchanged == 2 and r2.metadata_downloaded == 0


def test_default_strm_uses_115_scheme_and_delete_stale(tmp_path: Path):
    stale = tmp_path / "media" / "電影" / "Old" / "Old.strm"
    stale.parent.mkdir(parents=True)
    stale.write_text("115://zzzzzzzzzzzzzzzzz")
    keep = tmp_path / "media" / "電影" / "Old" / "mine.txt"
    keep.write_text("x")
    sync = make_sync(tmp_path, delete_stale=True, download_metadata=False)
    r = sync.run()
    assert (tmp_path / "media" / "劇集" / "Dark" / "Dark.S01E01.strm").read_text() == f"115://{PC_EP}"
    assert not stale.exists() and keep.exists()
    assert r.removed == 1


def test_missing_remote_dir_reports_error(tmp_path: Path):
    sync = make_sync(tmp_path)
    sync.cfg.tasks = [StrmTask(remote="/不存在", local=str(tmp_path / "x"))]
    r = sync.run()
    assert r.errors and "找不到目錄" in r.errors[0]
