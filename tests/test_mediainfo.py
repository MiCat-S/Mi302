"""媒體資訊：ffprobe → Emby 媒體流的對照、讀 X-mediainfo.json 回給播放器、同步時跟著 strm 搬和刪。"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from embyserver.app import create_app
from embyserver.config import config_from_dict
from embyserver.mediainfo import (
    build_sidecar, chapters, load_sidecar, map_probe, parse_sidecar, sidecar_path, write_sidecar,
)
from embyserver.strm_sync import _sidecars

PROBE = {
    "format": {"format_name": "matroska,webm", "duration": "3600.5", "size": "9000000000", "bit_rate": "19996000"},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "profile": "Main 10", "width": 3840, "height": 2160,
         "pix_fmt": "yuv420p10le", "color_transfer": "smpte2084", "color_primaries": "bt2020", "color_space": "bt2020nc",
         "avg_frame_rate": "24000/1001", "r_frame_rate": "24000/1001", "display_aspect_ratio": "16:9",
         "sample_aspect_ratio": "1:1", "field_order": "progressive", "disposition": {"default": 1}},
        {"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "channel_layout": "5.1(side)",
         "sample_rate": "48000", "bit_rate": "640000", "disposition": {"default": 0}, "tags": {"language": "chi"}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac", "channels": 2, "channel_layout": "stereo",
         "sample_rate": "48000", "disposition": {"default": 1}, "tags": {"language": "eng", "TITLE": "Stereo"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "width": 1920, "height": 1080,
         "disposition": {"forced": 1}, "tags": {"language": "chi"}},
        {"index": 4, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "chi", "title": "简中"}},
        {"index": 5, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
    ],
    "chapters": [{"start_time": "0.000000", "tags": {"title": "Opening"}}, {"start_time": "95.5005"}],
}


def test_map_probe_matches_emby_fields():
    ms = map_probe(PROBE, "/tv/Show/S01E01.mkv")
    assert (ms["Container"], ms["Size"], ms["RunTimeTicks"]) == ("mkv", 9000000000, 36005000000)
    assert ms["Bitrate"] == int(9000000000 * 8 * 1e7 / 36005000000)
    streams = ms["MediaStreams"]
    assert [s["Index"] for s in streams] == [0, 1, 2, 3, 4]  # 封面圖不算影片串流
    video, eac3, aac, pgs, ass = streams
    assert (video["Width"], video["Height"], video["BitDepth"]) == (3840, 2160, 10)
    assert (video["VideoRange"], video["ExtendedVideoType"], video["AspectRatio"]) == ("HDR 10", "Hdr10", "16:9")
    assert video["AverageFrameRate"] == 23.976025 and video["IsInterlaced"] is False
    assert (eac3["Channels"], eac3["ChannelLayout"], eac3["Language"]) == (6, "5.1", "chi")
    assert aac["BitRate"] == 192000 and aac["Title"] == "Stereo" and aac["IsDefault"]  # AAC 沒有碼率時用預設值
    assert (pgs["Codec"], pgs["IsTextSubtitleStream"], pgs["IsForced"]) == ("PGSSUB", False, True)
    assert ass["IsTextSubtitleStream"] and ass["SupportsExternalStream"]


def test_dolby_vision_and_bitrate_overflow():
    probe = {"format": {"format_name": "mov,mp4,m4a", "duration": "30", "size": str(2 * 10**12)}, "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
         "side_data_list": [{"side_data_type": "DOVI configuration record", "dv_profile": 8,
                             "dv_bl_signal_compatibility_id": 1}]},
    ]}
    ms = map_probe(probe, "/m/x.mp4")
    assert ms["Container"] == "mp4" and ms["Bitrate"] is None  # 超出 Int32 就不寫，免得 Emby 讀不了整份
    v = ms["MediaStreams"][0]
    assert (v["VideoRange"], v["ExtendedVideoSubType"], v["ExtendedVideoSubTypeDescription"]) == (
        "DolbyVision", "DoviProfile81", "Profile 8.1 (HDR10 compatible)")


def test_chapters_real_and_generated():
    assert chapters(PROBE) == [
        {"StartPositionTicks": 0, "Name": "Opening", "MarkerType": "Chapter", "ChapterIndex": 0},
        {"StartPositionTicks": 955000000, "Name": "章节 2", "MarkerType": "Chapter", "ChapterIndex": 1},
    ]
    # 沒有章節：每 300 秒一個
    fake = chapters({"format": {"duration": "650"}, "streams": []})
    assert [c["StartPositionTicks"] for c in fake] == [0, 3000000000, 6000000000]


def test_sidecar_round_trip_and_bad_files(tmp_path: Path):
    video = tmp_path / "S01E01.strm"
    video.write_text("http://x/a.mkv")
    sidecar = build_sidecar(PROBE, str(video))
    assert "Title" not in sidecar[0]["MediaSourceInfo"]["MediaStreams"][0]  # 空值不寫，和神醫一樣
    write_sidecar(video, sidecar)
    assert sidecar_path(video).name == "S01E01-mediainfo.json"
    info = load_sidecar(video)
    assert info["source"]["MediaStreams"][0]["Width"] == 3840 and len(info["chapters"]) == 2
    sidecar_path(video).write_text("{not json")
    assert load_sidecar(video) is None
    assert parse_sidecar([{"MediaSourceInfo": {"MediaStreams": "x"}}]) is None
    assert load_sidecar(tmp_path / "nothing.strm") is None


def test_playback_info_uses_sidecar(tmp_path: Path):
    show = tmp_path / "tv" / "Show (2020)"
    ep = show / "S01E01.strm"
    ep.parent.mkdir(parents=True)
    ep.write_text("http://cdn.example.com/a.mkv")
    write_sidecar(ep, build_sidecar(PROBE, str(ep)))
    (show / "S01E02.strm").write_text("http://cdn.example.com/b.mkv")  # 沒有媒體資訊的照舊
    app = create_app(config_from_dict({
        "server": {"data_dir": str(tmp_path / "data")},
        "users": [{"name": "admin", "password": "pw", "admin": True}],
        "libraries": [{"name": "劇集", "type": "tvshows", "paths": [str(tmp_path / "tv")]}],
    }), scan_on_start=False)
    app.state.scanner.scan_all()
    c = TestClient(app)
    h = {"X-Emby-Token": c.post("/Users/AuthenticateByName", json={"Username": "admin", "Pw": "pw"}).json()["AccessToken"]}
    series = c.get("/Items", params={"IncludeItemTypes": "Series", "Recursive": "true"}, headers=h).json()["Items"][0]
    eps = {e["IndexNumber"]: e for e in c.get(f"/Shows/{series['Id']}/Episodes", headers=h).json()["Items"]}
    assert eps[1]["RunTimeTicks"] == 36005000000  # nfo 沒有片長時用媒體資訊的

    pb = c.post(f"/Items/{eps[1]['Id']}/PlaybackInfo", headers=h).json()["MediaSources"][0]
    assert [s["Type"] for s in pb["MediaStreams"]] == ["Video", "Audio", "Audio", "Subtitle", "Subtitle"]
    assert pb["DefaultAudioStreamIndex"] == 2 and pb["Container"] == "mkv" and pb["Bitrate"]
    assert pb["SupportsTranscoding"] is False and pb["Protocol"] == "Http"  # 播放方式維持 302 直連

    full = c.get(f"/Items/{eps[1]['Id']}", headers=h).json()
    assert (full["Width"], full["Height"], full["HasSubtitles"]) == (3840, 2160, True)
    assert full["Chapters"][0]["Name"] == "Opening" and full["MediaStreams"][0]["VideoRange"] == "HDR 10"

    pb2 = c.post(f"/Items/{eps[2]['Id']}/PlaybackInfo", headers=h).json()["MediaSources"][0]
    assert pb2["MediaStreams"] == []

    # 改了 json 重新掃描會讀新的；刪掉項目時庫裡的也清掉
    probe = json.loads(json.dumps(PROBE))
    probe["streams"][0]["width"] = 1920
    import os
    import time
    write_sidecar(ep, build_sidecar(probe, str(ep)))
    os.utime(sidecar_path(ep), (time.time() + 5, time.time() + 5))
    app.state.scanner.scan_all()
    pb = c.post(f"/Items/{eps[1]['Id']}/PlaybackInfo", headers=h).json()["MediaSources"][0]
    assert pb["MediaStreams"][0]["Width"] == 1920
    ep.unlink()
    sidecar_path(ep).unlink()
    app.state.scanner.scan_all()
    assert app.state.db.one("SELECT COUNT(*) AS c FROM media_info")["c"] == 0


def test_sync_moves_and_deletes_mediainfo_with_strm(tmp_path: Path):
    folder = tmp_path / "Show"
    folder.mkdir()
    for name in ("X.strm", "X-mediainfo.json", "X.nfo", "X-2.strm", "X-2-mediainfo.json", "other.json"):
        (folder / name).write_text("x")
    assert sorted(f.name for f in _sidecars(folder, "X")) == ["X-mediainfo.json", "X.nfo"]  # X-2 的不算，別的 json 不動
