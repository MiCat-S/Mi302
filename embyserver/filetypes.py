"""各模組共用的副檔名清單，只在這裡定義一次。"""

from __future__ import annotations

# 真正的影片檔（115 上的檔案、strm 指向的目標）
VIDEO_EXTS = frozenset({
    ".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv",
    ".webm", ".rmvb", ".mpg", ".mpeg", ".iso", ".3gp",
})
# 媒體庫裡算「一部影片」的檔案：影片檔加上 strm
LIBRARY_VIDEO_EXTS = VIDEO_EXTS | {".strm"}
# 影音檔：資料夾裡有這些就不算空資料夾。比影片寬，寧可少列：DVD 的 vob、VCD 的 dat、其他少見的影片格式、音樂和外掛音軌
MEDIA_EXTS = LIBRARY_VIDEO_EXTS | frozenset({
    ".vob", ".dat", ".rm", ".asf", ".divx", ".f4v", ".mts", ".m2v", ".ogv", ".mk3d",
    ".mp3", ".flac", ".ape", ".wav", ".m4a", ".m4b", ".aac", ".ogg", ".opus", ".wma", ".dts", ".ac3", ".eac3",
    ".mka", ".dsf", ".dff", ".aiff", ".tak", ".tta", ".wv",
})
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
# 跟著影片一起從 115 下載的中繼資料：nfo、圖片、字幕
METADATA_EXTS = frozenset({".nfo", ".jpg", ".jpeg", ".png", ".webp", ".srt", ".ass", ".ssa", ".sup", ".vtt"})
