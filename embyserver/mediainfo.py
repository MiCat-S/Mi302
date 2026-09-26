"""媒體資訊（解析度、HDR、音軌、字幕軌、章節）：ffprobe 結果 → Emby 的 MediaSourceInfo。

存在影片旁邊的 X-mediainfo.json，格式和 Emby 神醫助手（StrmAssistant）「媒體資訊持久化」一樣：
[{"MediaSourceInfo": {...}, "Chapters": [...]}]。所以用過神醫、emby-mediainfo 產生的檔案可以直接讀，
Mi302 自己探測的結果 Emby 那邊也能共用。讀到的結果另外存一份在資料表 media_info，回給播放器時不必讀檔。

ffprobe → MediaSourceInfo 的對照改寫自 xiao-vvv/emby-mediainfo 的 app/mapper3.py，
規則來自大量神醫產出的統計，並逐欄位對過帳。原專案授權：

    MIT License

    Copyright (c) 2026 xiao-vvv

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .db import Database

log = logging.getLogger(__name__)

SIDECAR_SUFFIX = "-mediainfo.json"
MAX_SIDECAR_BYTES = 8 * 1024 * 1024
INT32_MAX = 2**31 - 1
INT64_MAX = 2**63 - 1
MAX_CHAPTERS = 2000
MAX_DURATION = 30 * 86400

SUB_CODEC = {"hdmv_pgs_subtitle": "PGSSUB", "dvd_subtitle": "DVDSUB", "dvb_subtitle": "DVBSUB"}
TEXT_SUBS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "srt", "text"}
AAC_DEFAULT = {1: 192000, 2: 192000, 6: 320000}
DOVI_DESC = {
    "50": "Profile 5.0", "81": "Profile 8.1 (HDR10 compatible)", "76": "Profile 7.6 (Bluray)",
    "84": "Profile 8.4 (HLG compatible)", "82": "Profile 8.2 (SDR compatible)",
}
KNOWN_AR_EXACT = {
    Fraction(3, 2): "1.5:1", Fraction(8, 5): "1.6:1", Fraction(37, 20): "1.85:1",
    Fraction(47, 20): "2.35:1", Fraction(12, 5): "2.40:1", Fraction(141, 100): "1.41:1",
}
# Emby 用正規化的容器名，不是副檔名；.f4v 只能查表（ffprobe 報成 mov,mp4…，Emby 認成 flv）
EXT_CONTAINER = {".f4v": "flv"}


# ---------------- ffprobe → MediaSourceInfo ----------------


def _f32(x: Optional[float]) -> Optional[float]:
    """C# float 的最短往返表示。"""
    if x is None:
        return None
    f = struct.unpack("f", struct.pack("f", x))[0]
    for p in range(1, 10):
        s = f"{f:.{p}g}"
        if struct.unpack("f", struct.pack("f", float(s)))[0] == f:
            return float(s)
    return f


def _frac(s: Any) -> Optional[float]:
    try:
        if not s or s in ("0/0", "N/A"):
            return None
        v = Fraction(s)
        return None if v == 0 else _f32(float(v))
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def _real_fps(st: dict) -> Optional[float]:
    """r_frame_rate 剛好是 avg 的兩倍（mpeg1/vc1 場頻）時 Emby 記的是 avg。"""
    r, a = _frac(st.get("r_frame_rate")), _frac(st.get("avg_frame_rate"))
    if r and a and abs(r - 2 * a) < 0.01:
        return a
    return r


def _container(fmt: dict, path: str) -> str:
    name = fmt.get("format_name") or ""
    ext = os.path.splitext(path or "")[1].lower()
    if ext in EXT_CONTAINER:
        return EXT_CONTAINER[ext]
    if name.startswith("matroska"):
        return "webm" if ext == ".webm" else "mkv"
    if name.startswith("mov,mp4"):
        return "mov" if ext == ".mov" else "mp4"
    return name.split(",")[0]


def _bitdepth_video(pix: Optional[str]) -> Optional[int]:
    if not pix:
        return None
    for d in ("16", "14", "12", "10", "9"):
        if d + "le" in pix or d + "be" in pix:
            return int(d)
    if pix.startswith(("rgb48", "rgba64", "bgr48", "bgra64")):
        return 16
    return 8


def _bitdepth_audio(st: dict) -> Optional[int]:
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        try:
            v = int(st.get(key) or 0)
        except (TypeError, ValueError):
            v = 0
        if v:
            return v
    return None


def _aspect(st: dict) -> Optional[str]:
    """有 DAR 用 DAR，沒有用 寬×SAR:高；先查精確比例表（3:2→1.5:1…），不中就約分原樣。"""
    dar = st.get("display_aspect_ratio")
    if dar and dar not in ("0:1", "N/A"):
        try:
            a, b = dar.split(":")
            fr = Fraction(int(a), int(b))
        except (ValueError, ZeroDivisionError):
            return dar
    else:
        w, h = st.get("width"), st.get("height")
        if not w or not h:
            return None
        num, den = w, h
        sar = st.get("sample_aspect_ratio")
        if sar and ":" in sar and sar not in ("0:1", "N/A"):
            try:
                a, b = sar.split(":")
                if int(a) > 0 and int(b) > 0:
                    num, den = num * int(a), den * int(b)
            except (TypeError, ValueError):
                pass
        try:
            fr = Fraction(num, den)
        except (ZeroDivisionError, TypeError, ValueError):
            return None
    return KNOWN_AR_EXACT.get(fr) or f"{fr.numerator}:{fr.denominator}"


def _video_range(st: dict) -> Tuple[str, Optional[dict]]:
    for sd in st.get("side_data_list") or []:
        if isinstance(sd, dict) and sd.get("side_data_type") == "DOVI configuration record":
            return "DolbyVision", sd
    transfer = st.get("color_transfer")
    if transfer == "smpte2084":
        return "HDR 10", None
    if transfer == "arib-std-b67":
        return "HLG", None
    return "SDR", None


def _ext_video(st: dict) -> Tuple[str, str, str]:
    vr, dovi = _video_range(st)
    if vr == "DolbyVision":
        profile, compat = dovi.get("dv_profile"), dovi.get("dv_bl_signal_compatibility_id")
        if profile is None:
            return "DolbyVision", "DolbyVision", "Dolby Vision"
        key = f"{profile}{compat if compat is not None else ''}"
        desc = DOVI_DESC.get(key, f"Profile {profile}.{compat}" if compat is not None else f"Profile {profile}")
        return "DolbyVision", f"DoviProfile{key}", desc
    if vr == "HDR 10":
        return "Hdr10", "Hdr10", "HDR 10"
    if vr == "HLG":
        return "HyperLogGamma", "HyperLogGamma", "HLG"
    return "None", "None", "None"


def _int(v: Any) -> Optional[int]:
    try:
        return int(v) if v not in (None, "", "N/A") else None
    except (TypeError, ValueError):
        return None


def duration_of(probe: dict) -> Optional[float]:
    """容器時長；沒有就退回串流時長或 mkv 的 DURATION 標籤。負數和離譜的值當沒有。"""
    fmt = probe.get("format") or {}
    dur: Optional[float] = None
    try:
        v = float(fmt.get("duration") or 0)
        if 0 < v <= MAX_DURATION:
            dur = v
    except (TypeError, ValueError):
        pass
    if not dur:
        for st in probe.get("streams") or []:
            try:
                d = float(st.get("duration") or 0)
            except (TypeError, ValueError):
                d = 0.0
            if not d:
                tags = st.get("tags") or {}
                text = tags.get("DURATION") or tags.get("DURATION-eng")
                try:
                    if text:
                        h, m, s = text.split(":")
                        d = int(h) * 3600 + int(m) * 60 + float(s)
                except (TypeError, ValueError):
                    d = 0.0
            if d > 0:
                dur = max(dur or 0.0, d)
    if dur and (dur <= 0 or dur > MAX_DURATION):
        return None
    return dur


def map_probe(probe: dict, path: str = "") -> dict:
    """ffprobe 的 JSON 輸出轉成 Emby 的 MediaSourceInfo（只含媒體本身的欄位）。"""
    fmt = probe.get("format") or {}
    size = _int(fmt.get("size"))
    dur = duration_of(probe)
    ticks = int(round(dur * 1e7)) if dur else None
    bitrate = int(size * 8 * 1e7 / ticks) if size and ticks else _int(fmt.get("bit_rate"))
    # Emby 的 Bitrate 是 int：超出 Int32 整份 json 會反序列化失敗，寧可不寫
    if bitrate is not None and not 0 < bitrate <= INT32_MAX:
        bitrate = None
    out: Dict[str, Any] = {
        "Container": _container(fmt, path), "Size": size, "Bitrate": bitrate, "RunTimeTicks": ticks,
        "Protocol": "File", "Type": "Default", "IsRemote": True, "AddApiKeyToDirectStreamUrl": False,
        "SupportsTranscoding": True, "SupportsDirectStream": True, "SupportsDirectPlay": True,
        "SupportsProbing": True, "IsInfiniteStream": False, "RequiresOpening": False, "RequiresClosing": False,
        "RequiresLooping": False, "ReadAtNativeFramerate": False, "HasMixedProtocols": False,
        "RequiredHttpHeaders": {}, "Formats": [], "Chapters": [], "MediaStreams": [],
    }
    for st in probe.get("streams") or []:
        kind = st.get("codec_type")
        disp = st.get("disposition") or {}
        tags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
        if kind == "video" and disp.get("attached_pic"):
            continue  # 封面圖不是影片串流
        s: Dict[str, Any] = {
            "Index": st.get("index"), "Codec": st.get("codec_name"), "IsDefault": bool(disp.get("default")),
            "IsForced": bool(disp.get("forced")), "IsHearingImpaired": bool(disp.get("hearing_impaired")),
            "Title": tags.get("title"), "Language": tags.get("language"), "Protocol": "File",
            "TimeBase": st.get("time_base"), "IsExternal": False, "IsInterlaced": False,
            "ExtendedVideoType": "None", "ExtendedVideoSubType": "None", "ExtendedVideoSubTypeDescription": "None",
            "IsTextSubtitleStream": False, "SupportsExternalStream": False,
            "BitRate": _int(st.get("bit_rate")), "AttachmentSize": 0,
        }
        if kind == "video":
            vr, _ = _video_range(st)
            et, est, desc = _ext_video(st)
            s.update({
                "Type": "Video", "Width": st.get("width"), "Height": st.get("height"), "Profile": st.get("profile"),
                "Level": st.get("level"), "PixelFormat": st.get("pix_fmt"), "BitDepth": _bitdepth_video(st.get("pix_fmt")),
                "ColorPrimaries": st.get("color_primaries"), "ColorSpace": st.get("color_space"),
                "ColorTransfer": st.get("color_transfer"), "VideoRange": vr, "ExtendedVideoType": et,
                "ExtendedVideoSubType": est, "ExtendedVideoSubTypeDescription": desc,
                "AverageFrameRate": _frac(st.get("avg_frame_rate")), "RealFrameRate": _real_fps(st),
                "RefFrames": st.get("refs"), "NalLengthSize": st.get("nal_length_size"),
                "IsInterlaced": st.get("field_order") not in (None, "progressive", "unknown"),
                "AspectRatio": _aspect(st),
                "IsAnamorphic": st.get("sample_aspect_ratio") not in (None, "1:1", "0:1", "N/A"),
                "BitRate": _int(st.get("bit_rate")) or bitrate,
            })
        elif kind == "audio":
            br = _int(st.get("bit_rate"))
            if br is None and st.get("codec_name") == "aac":
                br = AAC_DEFAULT.get(st.get("channels"))
            layout = st.get("channel_layout")
            s.update({
                "Type": "Audio", "Channels": st.get("channels"), "ChannelLayout": layout.split("(")[0] if layout else None,
                "SampleRate": _int(st.get("sample_rate")), "Profile": st.get("profile"),
                "BitDepth": _bitdepth_audio(st), "BitRate": br,
            })
        elif kind == "subtitle":
            codec = st.get("codec_name")
            s.update({
                "Type": "Subtitle", "Codec": SUB_CODEC.get(codec, codec), "SubtitleLocationType": "InternalStream",
                "IsTextSubtitleStream": codec in TEXT_SUBS, "SupportsExternalStream": codec in TEXT_SUBS,
            })
            if st.get("width"):
                s["Width"], s["Height"] = st.get("width"), st.get("height")
        elif kind == "attachment":
            name = tags.get("filename") or ""
            s.update({
                "Type": "Attachment", "Codec": os.path.splitext(name)[1].lstrip(".").lower() or None, "Path": name,
                "BitRate": None, "MimeType": tags.get("mimetype"),
                "AttachmentSize": _int(st.get("extradata_size")) or 0,
            })
        else:
            continue
        out["MediaStreams"].append(s)
    return out


def chapters(probe: dict) -> List[dict]:
    """真章節用標題和毫秒；沒有章節時 Emby 每 300 秒產生一個（個數 = 時長 // 300 + 1）。"""
    out: List[dict] = []
    found = probe.get("chapters") or []
    if found:
        for i, c in enumerate(found[:MAX_CHAPTERS]):
            try:
                start = float(c.get("start_time", 0) or 0)
            except (TypeError, ValueError):
                start = 0.0
            if start != start or start in (float("inf"), float("-inf")) or start < 0:
                start = 0.0
            # round() 是銀行家捨入，和 C# Math.Round 一致（四捨五入會和神醫差 1 毫秒）
            ticks = int(round(start * 1000)) * 10000
            if ticks > INT64_MAX:
                ticks = 0
            title = (c.get("tags") or {}).get("title")
            out.append({"StartPositionTicks": ticks, "Name": title or f"章节 {i + 1}", "MarkerType": "Chapter", "ChapterIndex": i})
        return out
    dur = duration_of(probe) or 0
    n = int(dur // 300) + 1 if dur > 0 else 0
    if n > MAX_CHAPTERS:
        n = 0
    return [
        {"StartPositionTicks": i * 300 * 10_000_000, "Name": f"章节 {i + 1}", "MarkerType": "Chapter", "ChapterIndex": i}
        for i in range(n)
    ]


def _strip(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if v is not None}
    if isinstance(o, list):
        return [_strip(x) for x in o]
    return o


def build_sidecar(probe: dict, path: str = "") -> list:
    """神醫 X-mediainfo.json 的完整結構。"""
    return _strip([{"MediaSourceInfo": map_probe(probe, path), "Chapters": chapters(probe)}])


# ---------------- X-mediainfo.json ----------------


def sidecar_path(video: Path) -> Path:
    return video.with_name(video.stem + SIDECAR_SUFFIX)


def is_sidecar(path: Path) -> bool:
    return path.name.lower().endswith(SIDECAR_SUFFIX)


def parse_sidecar(raw: Any) -> Optional[dict]:
    """神醫格式 → {"source": MediaSourceInfo, "chapters": [...]}；格式不對回傳 None。"""
    entry = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(entry, dict):
        return None
    source = entry.get("MediaSourceInfo")
    if not isinstance(source, dict) or not isinstance(source.get("MediaStreams"), list):
        return None
    source = dict(source)
    source["MediaStreams"] = [s for s in source["MediaStreams"] if isinstance(s, dict)]
    chaps = entry.get("Chapters")
    return {"source": source, "chapters": [c for c in chaps if isinstance(c, dict)] if isinstance(chaps, list) else []}


def load_sidecar(video: Path) -> Optional[dict]:
    path = sidecar_path(video)
    try:
        if path.stat().st_size > MAX_SIDECAR_BYTES:
            log.warning("媒體資訊檔太大，略過：%s", path)
            return None
        return parse_sidecar(json.loads(path.read_text(encoding="utf-8-sig")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("讀不懂媒體資訊檔 %s：%s", path, exc)
        return None


def write_sidecar(video: Path, sidecar: list) -> Path:
    """寫到影片旁邊；先寫暫存檔再改名，不會留下寫一半的檔案。"""
    path = sidecar_path(video)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(sidecar, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------- 回給播放器 ----------------


def primary_video(info: Optional[dict]) -> Optional[dict]:
    if not info:
        return None
    return next((s for s in info["source"].get("MediaStreams") or [] if s.get("Type") == "Video"), None)


def default_audio_index(info: Optional[dict]) -> Optional[int]:
    if not info:
        return None
    audio = [s for s in info["source"].get("MediaStreams") or [] if s.get("Type") == "Audio"]
    pick = next((s for s in audio if s.get("IsDefault")), audio[0] if audio else None)
    return pick.get("Index") if pick else None


class MediaInfoStore:
    """每支影片（以路徑為鍵）的媒體資訊；重新掃描、項目 id 變了都不受影響。"""

    def __init__(self, db: Database):
        self.db = db

    def get(self, path: str) -> Optional[dict]:
        row = self.db.one("SELECT data FROM media_info WHERE path=?", (path,))
        if not row:
            return None
        try:
            return json.loads(row["data"])
        except ValueError:
            return None

    def put(self, path: str, info: dict, mtime: float, source: str) -> None:
        self.db.execute(
            "INSERT INTO media_info(path, data, mtime, source, at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET data=excluded.data, mtime=excluded.mtime, source=excluded.source, at=excluded.at",
            (path, json.dumps(info, ensure_ascii=False), mtime, source, int(time.time())),
        )

    def sync_sidecar(self, video: Path) -> Optional[dict]:
        """掃描時呼叫：影片旁邊的 X-mediainfo.json 比庫裡新就讀進來。回傳目前的媒體資訊。"""
        try:
            mtime = sidecar_path(video).stat().st_mtime
        except OSError:
            return self.get(str(video))
        row = self.db.one("SELECT data, mtime FROM media_info WHERE path=?", (str(video),))
        if row and abs(row["mtime"] - mtime) < 1e-6:
            try:
                return json.loads(row["data"])
            except ValueError:
                pass
        info = load_sidecar(video)
        if info:
            self.put(str(video), info, mtime, "sidecar")
        return info

    def count(self) -> int:
        return self.db.one("SELECT COUNT(*) AS c FROM media_info m JOIN items i ON i.path=m.path")["c"]

    def prune(self) -> None:
        """項目刪掉了，媒體資訊也不必留（X-mediainfo.json 還在的話，下次掃描會再讀回來）。"""
        self.db.execute("DELETE FROM media_info WHERE path NOT IN (SELECT path FROM items WHERE path IS NOT NULL)")
