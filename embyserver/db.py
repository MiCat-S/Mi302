"""SQLite 資料層。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE COLLATE NOCASE,
    password_hash TEXT,
    is_admin INTEGER DEFAULT 0,
    last_login TEXT,
    last_activity TEXT
);

CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY,
    user_id TEXT,
    device_id TEXT,
    device_name TEXT,
    client TEXT,
    version TEXT,
    created TEXT,
    last_used TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER,
    parent_id INTEGER,
    type TEXT,
    collection_type TEXT,
    name TEXT,
    sort_name TEXT,
    original_title TEXT,
    path TEXT UNIQUE,
    is_strm INTEGER DEFAULT 0,
    container TEXT,
    size INTEGER,
    year INTEGER,
    premiere_date TEXT,
    overview TEXT,
    community_rating REAL,
    official_rating TEXT,
    genres TEXT,
    provider_ids TEXT,
    index_number INTEGER,
    parent_index_number INTEGER,
    series_id INTEGER,
    season_id INTEGER,
    runtime_ticks INTEGER,
    primary_image TEXT,
    backdrop_image TEXT,
    thumb_image TEXT,
    logo_image TEXT,
    date_created TEXT,
    date_modified TEXT,
    seen_scan INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_parent ON items(parent_id);
CREATE INDEX IF NOT EXISTS idx_items_library ON items(library_id);
CREATE INDEX IF NOT EXISTS idx_items_series ON items(series_id);
CREATE INDEX IF NOT EXISTS idx_items_type ON items(type);

CREATE TABLE IF NOT EXISTS user_data (
    user_id TEXT,
    item_id INTEGER,
    played INTEGER DEFAULT 0,
    play_count INTEGER DEFAULT 0,
    position_ticks INTEGER DEFAULT 0,
    is_favorite INTEGER DEFAULT 0,
    last_played TEXT,
    PRIMARY KEY (user_id, item_id)
);

-- 115 同步產生的本機檔案：115 的檔案／資料夾 id → 任務本機資料夾底下的相對路徑。
-- 生活事件只給 id，靠這張表找到移動、改名、刪除前的本機位置。
CREATE TABLE IF NOT EXISTS p115_index (
    task TEXT NOT NULL,
    file_id INTEGER NOT NULL,
    is_dir INTEGER NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (task, file_id)
);

-- 送去 MoviePilot 刮削後有 nfo 卻沒有劇照的集（多半是 TMDB 沒有這集的圖），一段時間內不再重送
-- 每支影片的媒體資訊（解析度、HDR、音軌、字幕軌、章節），來自旁邊的 X-mediainfo.json 或 ffprobe 探測
CREATE TABLE IF NOT EXISTS media_info (
    path TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    mtime REAL NOT NULL DEFAULT 0,
    source TEXT,
    at INTEGER
);

-- 演職人員（nfo 的演員、導演、編劇），每個項目一組；pid 是給播放器的人物 id（p{tmdbid} 或名稱雜湊）
CREATE TABLE IF NOT EXISTS people (
    item_id INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    name TEXT NOT NULL,
    role TEXT,
    type TEXT NOT NULL,
    tmdbid TEXT,
    thumb TEXT,
    pid TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_people_item ON people(item_id);
CREATE INDEX IF NOT EXISTS idx_people_pid ON people(pid);
CREATE INDEX IF NOT EXISTS idx_people_tmdb ON people(tmdbid);

-- 演職人員的中文名（TMDB 人物 id → 中文名）；查過沒有的 zh 是 NULL，30 天後再查
CREATE TABLE IF NOT EXISTS person_names (
    tmdbid TEXT PRIMARY KEY,
    zh TEXT,
    source TEXT,
    at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mp_no_image (
    path TEXT PRIMARY KEY,
    at INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            # WAL 加 NORMAL：資料庫不會壞，只是斷電可能少最後幾筆；每次 commit 不必 fsync，
            # 掃描時成千上萬筆寫入才不會把網頁的請求卡在同一把鎖後面
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(SCHEMA)
            self._ensure_columns()
            self.conn.commit()

    # 舊資料庫缺的欄位：CREATE TABLE IF NOT EXISTS 不會幫已存在的表加欄位
    COLUMNS = {"items": {"search_text": "TEXT"}}

    def _ensure_columns(self) -> None:
        for table, cols in self.COLUMNS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in cols.items():
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self.lock:
            self.conn.executemany(sql, (tuple(r) for r in rows))
            self.conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def get_meta(self, key: str) -> Optional[str]:
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_item(self, item_id: Any) -> Optional[sqlite3.Row]:
        try:
            iid = int(item_id)
        except (TypeError, ValueError):
            return None
        return self.one("SELECT * FROM items WHERE id=?", (iid,))
