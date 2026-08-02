# -*- coding: utf-8 -*-
"""SQLite 连接与表结构。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional

from .config import DB_PATH, ensure_dirs

_INIT_LOCK = threading.Lock()
_INITIALIZED = False

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS movies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT    NOT NULL UNIQUE,   -- 唯一归并键：番号或无番号时的路径指纹
    code            TEXT    DEFAULT '',        -- 展示用番号
    has_code        INTEGER DEFAULT 0,
    code_rule       TEXT    DEFAULT '',        -- 命中的识别规则名
    title           TEXT    DEFAULT '',
    original_title  TEXT    DEFAULT '',
    plot            TEXT    DEFAULT '',
    release_date    TEXT    DEFAULT '',
    year            INTEGER,
    runtime         INTEGER DEFAULT 0,         -- 分钟
    studio_id       INTEGER REFERENCES studios(id) ON DELETE SET NULL,
    publisher_id    INTEGER REFERENCES studios(id) ON DELETE SET NULL,
    series_id       INTEGER REFERENCES series(id)  ON DELETE SET NULL,
    director        TEXT    DEFAULT '',
    rating          REAL    DEFAULT 0,
    favorite        INTEGER DEFAULT 0,
    watched         INTEGER DEFAULT 0,
    play_count      INTEGER DEFAULT 0,
    last_played     TEXT    DEFAULT '',
    cover           TEXT    DEFAULT '',        -- data/covers 下的相对文件名
    fanart          TEXT    DEFAULT '',
    cover_source    TEXT    DEFAULT '',
    subtitle        INTEGER DEFAULT 0,         -- 中文字幕
    uncensored      INTEGER DEFAULT 0,
    leak            INTEGER DEFAULT 0,
    hd4k            INTEGER DEFAULT 0,
    vr              INTEGER DEFAULT 0,
    resolution      TEXT    DEFAULT '',        -- 解析出的分辨率标签 1080p/2160p/...
    size            INTEGER DEFAULT 0,         -- 所有分片总字节数
    file_count      INTEGER DEFAULT 0,
    folder          TEXT    DEFAULT '',
    scraped_at      TEXT    DEFAULT '',
    scrape_source   TEXT    DEFAULT '',
    note            TEXT    DEFAULT '',
    created_at      TEXT    DEFAULT (datetime('now','localtime')),
    updated_at      TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS movie_files (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    movie_id  INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
    path      TEXT    NOT NULL UNIQUE,
    filename  TEXT    DEFAULT '',
    ext       TEXT    DEFAULT '',
    size      INTEGER DEFAULT 0,
    mtime     REAL    DEFAULT 0,
    part      INTEGER DEFAULT 1,
    missing   INTEGER DEFAULT 0,
    quick_hash INTEGER DEFAULT 0            -- 内容指纹：size 与首尾采样哈希的 64 位摘要，用于精确去重
);

CREATE TABLE IF NOT EXISTS actresses (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,
    alias    TEXT DEFAULT '',
    avatar   TEXT DEFAULT '',
    birthday TEXT DEFAULT '',
    note     TEXT DEFAULT '',
    favorite INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS genres  (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS studios (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS series  (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS tags    (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);

CREATE TABLE IF NOT EXISTS movie_actress (
    movie_id   INTEGER NOT NULL REFERENCES movies(id)    ON DELETE CASCADE,
    actress_id INTEGER NOT NULL REFERENCES actresses(id) ON DELETE CASCADE,
    PRIMARY KEY (movie_id, actress_id)
);
CREATE TABLE IF NOT EXISTS movie_genre (
    movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
    genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    PRIMARY KEY (movie_id, genre_id)
);
CREATE TABLE IF NOT EXISTS movie_tag (
    movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id)   ON DELETE CASCADE,
    PRIMARY KEY (movie_id, tag_id)
);

CREATE TABLE IF NOT EXISTS scan_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    ended_at   TEXT,
    added      INTEGER DEFAULT 0,
    updated    INTEGER DEFAULT 0,
    removed    INTEGER DEFAULT 0,
    scanned    INTEGER DEFAULT 0,
    message    TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_movies_code    ON movies(code);
CREATE INDEX IF NOT EXISTS idx_movies_year    ON movies(year);
CREATE INDEX IF NOT EXISTS idx_movies_studio  ON movies(studio_id);
CREATE INDEX IF NOT EXISTS idx_movies_series  ON movies(series_id);
CREATE INDEX IF NOT EXISTS idx_movies_created ON movies(created_at);
CREATE INDEX IF NOT EXISTS idx_files_movie    ON movie_files(movie_id);
CREATE INDEX IF NOT EXISTS idx_files_size     ON movie_files(size);
CREATE INDEX IF NOT EXISTS idx_ma_actress     ON movie_actress(actress_id);
CREATE INDEX IF NOT EXISTS idx_mg_genre       ON movie_genre(genre_id);

CREATE TABLE IF NOT EXISTS watch_progress (
    movie_id   INTEGER PRIMARY KEY REFERENCES movies(id) ON DELETE CASCADE,
    position   REAL DEFAULT 0,
    duration   REAL DEFAULT 0,
    finished   INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS collections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    cover_movie_id INTEGER,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    movie_id   INTEGER NOT NULL REFERENCES movies(id)    ON DELETE CASCADE,
    position   INTEGER DEFAULT 0,
    PRIMARY KEY (collection_id, movie_id)
);

CREATE INDEX IF NOT EXISTS idx_wp_updated ON watch_progress(updated_at);
CREATE INDEX IF NOT EXISTS idx_ci_coll    ON collection_items(collection_id);

CREATE TABLE IF NOT EXISTS watch_sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    movie_id   INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
    started_at TEXT DEFAULT (datetime('now','localtime')),
    ended_at   TEXT,
    start_pos  REAL DEFAULT 0,
    end_pos    REAL DEFAULT 0,
    watched_sec REAL DEFAULT 0,
    finished   INTEGER DEFAULT 0,
    method     TEXT DEFAULT 'external',
    segments   TEXT
);

CREATE INDEX IF NOT EXISTS idx_ws_movie ON watch_sessions(movie_id);
"""


def init_db() -> None:
    global _INITIALIZED
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        ensure_dirs()
        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")  # 写前日志，提升并发读写吞吐
            conn.executescript(SCHEMA)
            conn.commit()
            # 兼容旧库的增量迁移（新字段）
            _migrate(conn)
        finally:
            conn.close()
        _INITIALIZED = True


def _migrate(conn: sqlite3.Connection) -> None:
    """为新版本新增的列做 ALTER 迁移，保证老库可平滑升级。"""
    movie_cols = {r[1] for r in conn.execute("PRAGMA table_info(movies)").fetchall()}
    if "resolution" not in movie_cols:
        conn.execute("ALTER TABLE movies ADD COLUMN resolution TEXT DEFAULT ''")
    if "watchlist" not in movie_cols:
        conn.execute("ALTER TABLE movies ADD COLUMN watchlist INTEGER DEFAULT 0")
    if "fanart_source" not in movie_cols:
        conn.execute("ALTER TABLE movies ADD COLUMN fanart_source TEXT DEFAULT ''")
    file_cols = {r[1] for r in conn.execute("PRAGMA table_info(movie_files)").fetchall()}
    if "quick_hash" not in file_cols:
        conn.execute("ALTER TABLE movie_files ADD COLUMN quick_hash INTEGER DEFAULT 0")
    # 依赖新列的索引统一在迁移阶段补齐（此时列已存在），避免旧库 executescript 时报错
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_hash ON movie_files(quick_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_movies_res ON movies(resolution)")
    # 新增的发现/片单相关表（已完成库也能平滑升级）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS watch_progress ("
        "movie_id INTEGER PRIMARY KEY REFERENCES movies(id) ON DELETE CASCADE, "
        "position REAL DEFAULT 0, duration REAL DEFAULT 0, finished INTEGER DEFAULT 0, "
        "updated_at TEXT DEFAULT (datetime('now','localtime')))")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS collections ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "cover_movie_id INTEGER, created_at TEXT DEFAULT (datetime('now','localtime')))")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS collection_items ("
        "collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE, "
        "movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE, "
        "position INTEGER DEFAULT 0, PRIMARY KEY (collection_id, movie_id))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wp_updated ON watch_progress(updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ci_coll    ON collection_items(collection_id)")
    # 观看历史 / 分段记录（A+B 档）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS watch_sessions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE, "
        "started_at TEXT DEFAULT (datetime('now','localtime')), ended_at TEXT, "
        "start_pos REAL DEFAULT 0, end_pos REAL DEFAULT 0, watched_sec REAL DEFAULT 0, "
        "finished INTEGER DEFAULT 0, method TEXT DEFAULT 'external', segments TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ws_movie ON watch_sessions(movie_id)")
    # 关注女优 / 智能清单 / 预览图墙
    actress_cols = {r[1] for r in conn.execute("PRAGMA table_info(actresses)").fetchall()}
    if "followed" not in actress_cols:
        conn.execute("ALTER TABLE actresses ADD COLUMN followed INTEGER DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_act_follow ON actresses(followed)")
    coll_cols = {r[1] for r in conn.execute("PRAGMA table_info(collections)").fetchall()}
    if "kind" not in coll_cols:
        conn.execute("ALTER TABLE collections ADD COLUMN kind TEXT DEFAULT 'manual'")
    if "rule" not in coll_cols:
        conn.execute("ALTER TABLE collections ADD COLUMN rule TEXT DEFAULT ''")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS movie_previews ("
        "movie_id INTEGER PRIMARY KEY REFERENCES movies(id) ON DELETE CASCADE, "
        "paths TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now','localtime')))")
    conn.commit()


def connect() -> sqlite3.Connection:
    init_db()
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """事务上下文：正常结束提交，异常回滚。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query_all(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def query_one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    row = conn.execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None


def scalar(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
    row = conn.execute(sql, tuple(params)).fetchone()
    if row is None or row[0] is None:
        return default
    return row[0]
