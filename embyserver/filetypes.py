"""各模組共用的副檔名清單，只在這裡定義一次。"""

from __future__ import annotations

# 真正的影片檔（115 上的檔案、strm 指向的目標）
VIDEO_EXTS = frozenset({
    ".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv",
    ".webm", ".rmvb", ".mpg", ".mpeg", ".iso", ".3gp",
})
# 媒體庫裡算「一部影片」的檔案：影片檔加上 strm
LIBRARY_VIDEO_EXTS = VIDEO_EXTS | {".strm"}
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
# 跟著影片一起從 115 下載的中繼資料：nfo、圖片、字幕
METADATA_EXTS = frozenset({".nfo", ".jpg", ".jpeg", ".png", ".webp", ".srt", ".ass", ".ssa", ".sup", ".vtt"})
