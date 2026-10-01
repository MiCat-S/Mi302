"""SQLite 資料層。"""

from __future__ import annotations

import os
import queue
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Optional

READERS = 4  # 同時最多幾條唯讀連線（查詢多於這個數就排隊）
# 同時最多幾個「拿整批結果」的查詢（query）。Python 把每一列轉成物件時要搶同一把 GIL，四個大查詢同時跑反而比排隊慢五倍
# （實測 44,000 列：排隊 0.37 秒、四個同時 2 秒）；兩個同時每個只慢一些，又不會被一個慢查詢全部卡住。
# 只拿一列的查詢（one、scalar、get_item：驗 token、查項目）不算在內，永遠有連線可用。
BULK_READS = 2


def make_private(path: Path | str) -> None:
    """只給執行 Mi302 的帳號讀寫（0600）。資料庫、備份、設定檔裡有 115 的 cookie、登入 token、密碼雜湊和明碼密碼，
    照預設的 umask 022 建出來是 0644，同一台機器上的其他帳號都讀得到。改不了（別人的檔案、不支援的檔案系統）就算了。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


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
CREATE INDEX IF NOT EXISTS idx_items_season ON items(season_id);
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
CREATE INDEX IF NOT EXISTS idx_p115_file ON p115_index(file_id);  -- 由 115 檔案 id 找本機的 strm（瀏覽 115、重複檔案）

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

-- 片頭片尾：從播放行為學到的紀錄（每個使用者對每一集各留一筆），給播放器「跳過片頭」用
CREATE TABLE IF NOT EXISTS intro_obs (
    item_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    start_ticks INTEGER NOT NULL,
    end_ticks INTEGER NOT NULL,
    at INTEGER NOT NULL,
    PRIMARY KEY (item_id, user_id, kind)
);

-- 手動設定的片頭片尾，每一季一列；設了的季不用學到的值。
-- mode：auto = 照播放行為學、manual = 用這裡的值、none = 這一季沒有；時間都是 ticks，片尾記「從結尾前多久開始」
CREATE TABLE IF NOT EXISTS intro_manual (
    season_id INTEGER PRIMARY KEY,
    intro_mode TEXT NOT NULL DEFAULT 'auto',
    intro_start INTEGER,
    intro_end INTEGER,
    credits_mode TEXT NOT NULL DEFAULT 'auto',
    credits_tail INTEGER,
    at INTEGER NOT NULL
);

-- 115 上內容完全相同（SHA1 和大小都一樣）的影片，「找重複」時整批重建，只存有重複的。
-- path 是 115 上的完整路徑，local 是本機的 strm（在同步任務裡才有），keep = 建議保留
CREATE TABLE IF NOT EXISTS dup_files (
    file_id INTEGER PRIMARY KEY,
    sha1 TEXT NOT NULL,
    size INTEGER NOT NULL,
    name TEXT NOT NULL,
    pickcode TEXT,
    parent_id INTEGER,
    path TEXT,
    local TEXT,
    mtime INTEGER,
    keep INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dup_group ON dup_files(sha1, size);

-- 同一部片的不同版本（畫質、字幕組、編碼不同）：同一部電影（tmdbid，或片名＋年份）或同一集，檔案不同。
-- grp 是分組的鍵，title 是顯示的片名；quality 是 JSON（解析度、HDR、編碼、音軌、字幕）；keep = 建議保留
CREATE TABLE IF NOT EXISTS dup_versions (
    file_id INTEGER PRIMARY KEY,
    grp TEXT NOT NULL,
    title TEXT NOT NULL,
    item_id INTEGER,
    name TEXT NOT NULL,
    path TEXT,
    local TEXT,
    size INTEGER,
    mtime INTEGER,
    sha1 TEXT,
    quality TEXT,
    keep INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dupv_grp ON dup_versions(grp);

-- 找重複時順便記下的大檔案（1 GB 以上的影片），「大檔案」篩選、刪除用。type 是媒體庫裡的 Movie／Episode，
-- 不在媒體庫是 NULL；quality 是 JSON（解析度、HDR、編碼、音軌、字幕）
CREATE TABLE IF NOT EXISTS big_files (
    file_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT,
    local TEXT,
    type TEXT,
    size INTEGER NOT NULL,
    mtime INTEGER,
    sha1 TEXT,
    quality TEXT
);
CREATE INDEX IF NOT EXISTS idx_big_size ON big_files(size);

-- 刪掉的重複檔案、大檔案（在 115 回收站找回用）
CREATE TABLE IF NOT EXISTS dup_deleted (
    file_id INTEGER,
    sha1 TEXT,
    size INTEGER,
    name TEXT,
    path TEXT,
    at INTEGER NOT NULL
);

-- 送去 MoviePilot 刮削後有 nfo 卻沒有劇照的集（多半是 TMDB 沒有這集的圖），一段時間內不再重送
CREATE TABLE IF NOT EXISTS mp_no_image (
    path TEXT PRIMARY KEY,
    at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mp_no_image_at ON mp_no_image(at);  -- 找最近送過的、清掉過期的

-- 整理 115 網盤：問過 MoviePilot「這個資料夾（和幾支影片）整理後叫什麼」的結果；裡面的影片沒變就不再問
CREATE TABLE IF NOT EXISTS organize_checks (
    path TEXT PRIMARY KEY,  -- 115 上的資料夾（沒有自己資料夾的電影是那支影片）
    sig TEXT NOT NULL,      -- 名稱和裡面影片清單的雜湊
    name TEXT,              -- MoviePilot 給的資料夾名稱
    files TEXT,             -- JSON：[[現在的檔名, MoviePilot 給的檔名], ...]
    error TEXT,             -- MoviePilot 認不出來時的說明
    at INTEGER NOT NULL
);

-- 115 上的空資料夾（底下沒有影音檔，只留最外層）：「掃描」時整批重建，刪掉的拿掉
CREATE TABLE IF NOT EXISTS empty_dirs (
    cid INTEGER PRIMARY KEY,
    parent_cid INTEGER NOT NULL,  -- 上一層的 id（刪之前確認它還在那裡）
    path TEXT NOT NULL,           -- 115 上的完整路徑
    files INTEGER NOT NULL DEFAULT 0,  -- 裡面（含子資料夾）有幾個檔案、幾個資料夾、檔案共多大
    dirs INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    sample TEXT,                  -- JSON：裡面前幾個檔名（相對路徑）
    mtime INTEGER                 -- 115 上資料夾的修改時間
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            # 先用 0600 建好檔案：SQLite 建 -wal、-shm 時照主檔的權限
            os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
            for name in (self.path, self.path + "-wal", self.path + "-shm"):
                if os.path.exists(name):
                    make_private(name)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        # 讀寫分開：寫（和交易）照舊走 self.conn、拿 self.lock；查詢走另外幾條唯讀連線。WAL 模式下讀不必等寫、
        # 也不必等別的讀，一個慢查詢不會再把所有請求卡在同一把鎖後面。記憶體資料庫（測試）只有一條連線，照舊。
        self._readers: "queue.LifoQueue[sqlite3.Connection]" = queue.LifoQueue()
        self._reader_conns: List[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        self._closed = False
        self._bulk = threading.BoundedSemaphore(BULK_READS)
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
    # ep_from：集號從哪裡來（nfo、sxe = 標準檔名 S01E02、name = 其他檔名寫法（猜的）、none = 認不出來）
    COLUMNS = {"items": {"search_text": "TEXT", "ep_from": "TEXT"}}
    # 用到上面這些欄位的索引，欄位補上之後才能建
    COLUMN_INDEXES = ["CREATE INDEX IF NOT EXISTS idx_items_epfrom ON items(ep_from)"]

    def _ensure_columns(self) -> None:
        for table, cols in self.COLUMNS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in cols.items():
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        for sql in self.COLUMN_INDEXES:
            self.conn.execute(sql)

    def close(self) -> None:
        """程式結束時關閉連線，WAL 裡的內容併回資料庫檔。之後再查會丟 sqlite3.ProgrammingError。"""
        with self._readers_lock:
            self._closed = True
            readers, self._reader_conns = self._reader_conns, []
        for conn in readers:  # 唯讀的先關，最後一條連線（寫的）關的時候才會把 WAL 併回去
            conn.close()
        with self.lock:
            self.conn.close()

    def _open_reader(self) -> sqlite3.Connection:
        # 路徑跳脫成 URI（資料夾名稱可能有 # 或 ?）；query_only：這幾條連線保證不會寫
        conn = sqlite3.connect(Path(self.path).resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA query_only=1")
        return conn

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        """借一條連線查詢。看到的是已經 commit 的資料：self.lock 裡還沒 commit 的寫入，別的查詢看不到（看到的是舊的）。"""
        if self.path == ":memory:":
            with self.lock:
                yield self.conn
            return
        try:
            conn = self._readers.get_nowait()
        except queue.Empty:
            with self._readers_lock:
                if self._closed:
                    raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
                conn = self._open_reader() if len(self._reader_conns) < READERS else None
                if conn is not None:
                    self._reader_conns.append(conn)
            if conn is None:
                conn = self._readers.get()  # 都借出去了：等一條還回來
        try:
            yield conn
        finally:
            self._readers.put(conn)

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
        with self._bulk, self._reading() as conn:
            return conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self._reading() as conn:
            cur = conn.execute(sql, tuple(params))
            row = cur.fetchone()
            cur.close()  # 沒讀完的查詢會一直佔著讀取快照，WAL 就併不回去
            return row

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        """只要第一列的第一個欄位（例如 COUNT(*)）；查不到任何列時回傳 None。"""
        row = self.one(sql, params)
        return row[0] if row else None

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
