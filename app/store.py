# -*- coding: utf-8 -*-
"""数据访问层：影片的增删改查、分类归并、检索与统计。"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import subtitles
from .db import query_all, query_one, scalar

_TAXONOMY = {"actresses", "genres", "studios", "series", "tags"}

_REL_META = {
    "actress": ("movie_actress", "actress_id", "actresses"),
    "genre": ("movie_genre", "genre_id", "genres"),
    "tag": ("movie_tag", "tag_id", "tags"),
}

SORTS = {
    "added_desc": "m.created_at DESC, m.id DESC",
    "added_asc": "m.created_at ASC, m.id ASC",
    "code_asc": "m.code ASC, m.id ASC",
    "code_desc": "m.code DESC, m.id DESC",
    "date_desc": "(m.release_date = '') ASC, m.release_date DESC",
    "date_asc": "(m.release_date = '') ASC, m.release_date ASC",
    "size_desc": "m.size DESC",
    "size_asc": "m.size ASC",
    "rating_desc": "m.rating DESC, m.id DESC",
    "title_asc": "m.title ASC",
    "play_desc": "m.play_count DESC, m.id DESC",
    "random": "RANDOM()",
    "actress_count_desc": "(SELECT COUNT(*) FROM movie_actress mc WHERE mc.movie_id = m.id) DESC, m.id DESC",
}

FLAG_CLAUSES = {
    "subtitle": "m.subtitle = 1",
    "uncensored": "m.uncensored = 1",
    "leak": "m.leak = 1",
    "hd4k": "m.hd4k = 1",
    "vr": "m.vr = 1",
    "favorite": "m.favorite = 1",
    "watched": "m.watched = 1",
    "watchlist": "m.watchlist = 1",
    "unwatched": "m.watched = 0",
    "hascover": "m.cover <> ''",
    "nocover": "m.cover = ''",
    "scraped": "m.scraped_at <> ''",
    "noscrape": "m.scraped_at = ''",
    "nocode": "m.has_code = 0",
    "multi": "m.id IN (SELECT movie_id FROM movie_actress GROUP BY movie_id HAVING COUNT(*) >= 2)",
}


def norm_name(name: Any) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip()


# 分辨率质量排序，用于同番号多版本里挑选「最佳版」
_RES_ORDER = ["", "480p", "720p", "1080p", "1440p", "2160p"]


def _res_rank(label: str) -> int:
    try:
        return _RES_ORDER.index(label)
    except ValueError:
        return 0


# ----------------------------------------------------------------- 分类表


def get_or_create(conn: sqlite3.Connection, table: str, name: Any) -> Optional[int]:
    if table not in _TAXONOMY:
        raise ValueError(f"非法分类表: {table}")
    name = norm_name(name)
    if not name:
        return None
    row = conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    return conn.execute(f"INSERT INTO {table}(name) VALUES(?)", (name,)).lastrowid


def ensure_tag(conn: sqlite3.Connection, name: Any) -> Optional[int]:
    """标签辅助：等价于 get_or_create(conn, 'tags', name)。"""
    return get_or_create(conn, "tags", name)


def set_relations(conn: sqlite3.Connection, movie_id: int, kind: str,
                  names: Iterable[Any], replace: bool = True) -> None:
    link_table, col, tax_table = _REL_META[kind]
    if replace:
        conn.execute(f"DELETE FROM {link_table} WHERE movie_id = ?", (movie_id,))
    for raw in names or []:
        rid = get_or_create(conn, tax_table, raw)
        if rid:
            conn.execute(
                f"INSERT OR IGNORE INTO {link_table}(movie_id, {col}) VALUES(?, ?)",
                (movie_id, rid),
            )


def get_relations(conn: sqlite3.Connection, movie_id: int, kind: str) -> List[str]:
    link_table, col, tax_table = _REL_META[kind]
    rows = conn.execute(
        f"SELECT t.name FROM {link_table} l JOIN {tax_table} t ON t.id = l.{col} "
        f"WHERE l.movie_id = ? ORDER BY t.name",
        (movie_id,),
    ).fetchall()
    return [r[0] for r in rows]


# ----------------------------------------------------------------- 扫描入库


def upsert_scanned_file(conn: sqlite3.Connection, parsed: Dict[str, Any],
                        path: str, size: int, mtime: float, quick_hash: int = 0) -> str:
    """把一个扫描到的视频文件写入库，返回 'added' / 'updated' / 'unchanged'。"""
    movie = query_one(conn, "SELECT * FROM movies WHERE key = ?", (parsed["key"],))
    if movie is None:
        cur = conn.execute(
            """INSERT INTO movies(key, code, has_code, code_rule, title, folder,
                                  subtitle, uncensored, leak, hd4k, vr, resolution)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                parsed["key"], parsed["code"], parsed["has_code"], parsed["code_rule"],
                parsed["title"], parsed["folder"], parsed["subtitle"],
                parsed["uncensored"], parsed["leak"], parsed["hd4k"], parsed["vr"],
                parsed["resolution"],
            ),
        )
        movie_id = int(cur.lastrowid)
    else:
        movie_id = int(movie["id"])
        # 同一番号的不同版本：布尔标记取并集，分辨率取较高者
        new_res = parsed["resolution"]
        cur_res = movie["resolution"] or ""
        best_res = new_res if _res_rank(new_res) >= _res_rank(cur_res) else cur_res
        conn.execute(
            """UPDATE movies SET subtitle = MAX(subtitle, ?), uncensored = MAX(uncensored, ?),
                                 leak = MAX(leak, ?), hd4k = MAX(hd4k, ?), vr = MAX(vr, ?),
                                 resolution = ?
               WHERE id = ?""",
            (parsed["subtitle"], parsed["uncensored"], parsed["leak"],
             parsed["hd4k"], parsed["vr"], best_res, movie_id),
        )

    existing = query_one(conn, "SELECT * FROM movie_files WHERE path = ?", (path,))
    if existing is None:
        conn.execute(
            """INSERT INTO movie_files(movie_id, path, filename, ext, size, mtime, part, missing, quick_hash)
               VALUES(?,?,?,?,?,?,?,0,?)""",
            (movie_id, path, parsed["filename"], parsed["ext"], size, mtime,
             parsed["part"], quick_hash),
        )
        result = "added"
    elif abs(float(existing["size"] or 0) - size) > 0.5 or abs(float(existing["mtime"] or 0) - mtime) > 1 \
            or existing["missing"] or int(existing.get("quick_hash") or 0) != quick_hash:
        conn.execute(
            "UPDATE movie_files SET size = ?, mtime = ?, missing = 0, movie_id = ?, quick_hash = ? WHERE id = ?",
            (size, mtime, movie_id, quick_hash, existing["id"]),
        )
        result = "updated"
    else:
        result = "unchanged"

    refresh_aggregate(conn, movie_id)
    return result


def refresh_aggregate(conn: sqlite3.Connection, movie_id: int) -> None:
    row = conn.execute(
        "SELECT COALESCE(SUM(size),0), COUNT(*) FROM movie_files WHERE movie_id = ? AND missing = 0",
        (movie_id,),
    ).fetchone()
    conn.execute(
        "UPDATE movies SET size = ?, file_count = ?, updated_at = datetime('now','localtime') WHERE id = ?",
        (int(row[0]), int(row[1]), movie_id),
    )


def prune_missing(conn: sqlite3.Connection, alive_paths: set[str], roots: Sequence[str]) -> int:
    """删除位于扫描根目录下、但磁盘上已不存在的文件记录，并清理空影片。"""
    if not roots:
        return 0
    removed = 0
    lowered_roots = [r.replace("\\", "/").lower().rstrip("/") for r in roots]
    for row in conn.execute("SELECT id, path, movie_id FROM movie_files").fetchall():
        p = row["path"].replace("\\", "/").lower()
        if not any(p.startswith(r + "/") or p == r for r in lowered_roots):
            continue
        if row["path"] in alive_paths:
            continue
        conn.execute("DELETE FROM movie_files WHERE id = ?", (row["id"],))
        removed += 1
    conn.execute("DELETE FROM movies WHERE id NOT IN (SELECT DISTINCT movie_id FROM movie_files)")
    return removed


# ----------------------------------------------------------------- 检索


_LIST_SELECT = """
SELECT m.id, m.key, m.code, m.has_code, m.title, m.original_title, m.release_date, m.year,
       m.runtime, m.rating, m.favorite, m.watched, m.watchlist, m.play_count, m.cover, m.size,
       m.file_count, m.subtitle, m.uncensored, m.leak, m.hd4k, m.vr, m.scraped_at,
       m.created_at, m.folder,
       st.name AS studio, se.name AS series,
       (SELECT group_concat(a.name, '||') FROM movie_actress ma
          JOIN actresses a ON a.id = ma.actress_id WHERE ma.movie_id = m.id) AS actress_str,
       (SELECT group_concat(g.name, '||') FROM movie_genre mg
          JOIN genres g ON g.id = mg.genre_id WHERE mg.movie_id = m.id) AS genre_str,
       COALESCE(wp.position, 0) AS progress_seconds,
       COALESCE(wp.duration, 0) AS duration_seconds,
       COALESCE(wp.finished, 0) AS progress_finished
FROM movies m
LEFT JOIN studios st ON st.id = m.studio_id
LEFT JOIN series  se ON se.id = m.series_id
LEFT JOIN watch_progress wp ON wp.movie_id = m.id
"""


def _build_where(params: Dict[str, Any]) -> Tuple[str, List[Any]]:
    clauses: List[str] = []
    args: List[Any] = []
    op = " OR " if str(params.get("op") or "AND").upper() == "OR" else " AND "

    q = norm_name(params.get("q"))
    if q:
        like = f"%{q}%"
        clauses.append(
            "(m.code LIKE ? OR m.title LIKE ? OR m.original_title LIKE ? OR m.plot LIKE ?"
            " OR m.director LIKE ? OR m.folder LIKE ?"
            " OR EXISTS(SELECT 1 FROM movie_actress ma JOIN actresses a ON a.id = ma.actress_id"
            "           WHERE ma.movie_id = m.id AND a.name LIKE ?)"
            " OR EXISTS(SELECT 1 FROM movie_files f WHERE f.movie_id = m.id AND f.filename LIKE ?))"
        )
        args.extend([like] * 8)

    for kind in ("actress", "genre", "tag"):
        sub = _multi_clause(kind, params.get(kind), op, args)
        if sub:
            clauses.append(sub)

    studio = norm_name(params.get("studio"))
    if studio:
        clauses.append("st.name = ?")
        args.append(studio)

    series_name = norm_name(params.get("series"))
    if series_name:
        clauses.append("se.name = ?")
        args.append(series_name)

    year = params.get("year")
    if year:
        clauses.append("m.year = ?")
        args.append(int(year))

    prefix = norm_name(params.get("prefix"))
    if prefix:
        clauses.append("m.code LIKE ?")
        args.append(f"{prefix}-%")

    for flag in _split_multi(params.get("flags")):
        clause = FLAG_CLAUSES.get(flag)
        if clause:
            clauses.append(clause)

    # 顶层布尔 flag（智能清单规则以 {unwatched:1, favorite:1, ...} 形式直接传入）
    for fk, clause in FLAG_CLAUSES.items():
        val = params.get(fk)
        if val in (1, "1", True, "true", "on"):
            clauses.append(clause)

    # 评分区间
    if "min_rating" in params and params["min_rating"] not in (None, ""):
        clauses.append("m.rating >= ?")
        args.append(float(params["min_rating"]))
    if "max_rating" in params and params["max_rating"] not in (None, ""):
        clauses.append("m.rating <= ?")
        args.append(float(params["max_rating"]))

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, args


def _multi_clause(kind: str, value: Any, op: str, args: List[Any]) -> Optional[str]:
    """为女优/类型/标签等多值筛选项生成 (EXISTS(...) OP EXISTS(...)) 子句。

    op 控制多值之间的逻辑关系：AND=全部满足，OR=任一满足。"""
    names = _split_multi(value)
    if not names:
        return None
    table = {
        "actress": ("movie_actress", "actresses", "actress_id"),
        "genre": ("movie_genre", "genres", "genre_id"),
        "tag": ("movie_tag", "tags", "tag_id"),
    }[kind]
    subs: List[str] = []
    for n in names:
        subs.append(
            f"EXISTS(SELECT 1 FROM {table[0]} x JOIN {table[1]} y ON y.id = x.{table[2]}"
            f" WHERE x.movie_id = m.id AND y.name = ?)"
        )
        args.append(n)
    return "(" + op.join(subs) + ")"


def _split_multi(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = str(value).split(",")
    return [norm_name(i) for i in items if norm_name(i)]


def _row_to_card(row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row)
    row["actresses"] = [x for x in (row.pop("actress_str", None) or "").split("||") if x]
    row["genres"] = [x for x in (row.pop("genre_str", None) or "").split("||") if x]
    row["studio"] = row.get("studio") or ""
    row["series"] = row.get("series") or ""
    row["watchlist"] = int(row.get("watchlist") or 0)
    row["display_code"] = row["code"] if row["has_code"] else ""
    return row


def find_movies(conn: sqlite3.Connection, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """全量检索别名：返回与 search_movies 相同的结构（默认拉取前 500 条）。CLI 导出等场景使用。"""
    params = dict(params or {})
    params.setdefault("page_size", 500)
    return search_movies(conn, params)


def search_movies(conn: sqlite3.Connection, params: Dict[str, Any]) -> Dict[str, Any]:
    where, args = _build_where(params)
    order = SORTS.get(str(params.get("sort") or "added_desc"), SORTS["added_desc"])
    page = max(1, int(params.get("page") or 1))
    size = min(500, max(1, int(params.get("page_size") or 60)))
    offset = (page - 1) * size

    total = int(scalar(
        conn,
        "SELECT COUNT(*) FROM movies m "
        "LEFT JOIN studios st ON st.id = m.studio_id "
        "LEFT JOIN series se ON se.id = m.series_id" + where,
        args,
    ))
    rows = query_all(
        conn,
        f"{_LIST_SELECT}{where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*args, size, offset],
    )
    return {
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, (total + size - 1) // size),
        "items": [_row_to_card(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# 观看历史 / 分段记录（A+B 档）
# ---------------------------------------------------------------------------

def _j(v):
    return json.dumps(v, ensure_ascii=False) if v is not None else None


def _parsej(v):
    if not v:
        return []
    try:
        return json.loads(v)
    except Exception:
        return []


def start_session(conn: sqlite3.Connection, movie_id: int, start_pos: float = 0.0,
                  method: str = 'external') -> int:
    cur = conn.execute(
        "INSERT INTO watch_sessions (movie_id, start_pos, method, started_at) "
        "VALUES (?,?,?,datetime('now','localtime'))",
        (int(movie_id), float(start_pos or 0), method))
    return cur.lastrowid


def update_session(conn: sqlite3.Connection, session_id: int, watched_sec: float,
                   segments) -> None:
    conn.execute(
        "UPDATE watch_sessions SET watched_sec=?, segments=? WHERE id=?",
        (float(watched_sec or 0), _j(segments), int(session_id)))


def end_session(conn: sqlite3.Connection, session_id: int, end_pos: float,
                watched_sec: float, finished: int = 0, segments=None) -> None:
    conn.execute(
        "UPDATE watch_sessions SET ended_at=datetime('now','localtime'), end_pos=?, "
        "watched_sec=?, finished=?, segments=? WHERE id=?",
        (float(end_pos or 0), float(watched_sec or 0), int(finished or 0),
         _j(segments), int(session_id)))


def movie_sessions(conn: sqlite3.Connection, movie_id: int, limit: int = 60) -> list:
    rows = query_all(conn,
        "SELECT * FROM watch_sessions WHERE movie_id=? ORDER BY started_at DESC LIMIT ?",
        (int(movie_id), int(limit)))
    for r in rows:
        r['segments'] = _parsej(r.get('segments'))
    return rows


def movie_primary_file(conn: sqlite3.Connection, movie_id: int):
    row = query_one(conn,
        "SELECT path FROM movie_files WHERE movie_id=? ORDER BY part ASC, size DESC LIMIT 1",
        (int(movie_id),))
    return row['path'] if row else None


def watch_analytics(conn: sqlite3.Connection) -> Dict[str, Any]:
    """基于真实观看时长，分析用户的观影偏好与习惯。"""
    tot = query_one(conn, """
        SELECT COUNT(*) AS sessions, COALESCE(SUM(watched_sec),0) AS total_sec,
               COUNT(DISTINCT movie_id) AS movies,
               COUNT(DISTINCT DATE(started_at)) AS days,
               MIN(started_at) AS first_at, MAX(started_at) AS last_at
        FROM watch_sessions""") or {}
    sessions = int(tot.get('sessions') or 0)
    total_sec = float(tot.get('total_sec') or 0)

    by_hour = [0.0] * 24
    for r in query_all(conn,
        "SELECT CAST(strftime('%H', started_at) AS INTEGER) AS h, "
        "COALESCE(SUM(watched_sec),0) AS ws FROM watch_sessions GROUP BY h"):
        by_hour[r['h']] = float(r['ws'])

    def profile(sql):
        return [{'name': x['name'], 'sec': float(x['ws'])} for x in query_all(conn, sql)]

    genres = profile(
        "SELECT g.name AS name, SUM(s.watched_sec) AS ws FROM watch_sessions s "
        "JOIN movie_genre mg ON mg.movie_id=s.movie_id JOIN genres g ON g.id=mg.genre_id "
        "GROUP BY g.id ORDER BY ws DESC LIMIT 15")
    actresses = profile(
        "SELECT a.name AS name, SUM(s.watched_sec) AS ws FROM watch_sessions s "
        "JOIN movie_actress ma ON ma.movie_id=s.movie_id JOIN actresses a ON a.id=ma.actress_id "
        "GROUP BY a.id ORDER BY ws DESC LIMIT 15")
    studios = profile(
        "SELECT st.name AS name, SUM(s.watched_sec) AS ws FROM watch_sessions s "
        "JOIN movies m ON m.id=s.movie_id JOIN studios st ON st.id=m.studio_id "
        "WHERE m.studio_id IS NOT NULL GROUP BY st.id ORDER BY ws DESC LIMIT 15")
    series = profile(
        "SELECT se.name AS name, SUM(s.watched_sec) AS ws FROM watch_sessions s "
        "JOIN movies m ON m.id=s.movie_id JOIN series se ON se.id=m.series_id "
        "WHERE m.series_id IS NOT NULL GROUP BY se.id ORDER BY ws DESC LIMIT 15")
    directors = profile(
        "SELECT m.director AS name, SUM(s.watched_sec) AS ws FROM watch_sessions s "
        "JOIN movies m ON m.id=s.movie_id WHERE m.director IS NOT NULL AND m.director<>'' "
        "GROUP BY m.director ORDER BY ws DESC LIMIT 15")

    recent = query_all(conn, """
        SELECT s.id, s.movie_id, s.started_at, s.watched_sec, s.finished, s.method, s.segments,
               m.code, m.title, m.cover
        FROM watch_sessions s JOIN movies m ON m.id=s.movie_id
        ORDER BY s.started_at DESC LIMIT 30""")
    for r in recent:
        r['segments'] = _parsej(r.get('segments'))

    top_movies = query_all(conn, """
        SELECT m.id, m.code, m.title, m.cover,
               COALESCE(SUM(s.watched_sec),0) AS total_sec, COUNT(*) AS sessions
        FROM watch_sessions s JOIN movies m ON m.id=s.movie_id
        GROUP BY m.id ORDER BY total_sec DESC LIMIT 12""")

    return {
        'total_sec': total_sec,
        'sessions': sessions,
        'movies': int(tot.get('movies') or 0),
        'days': int(tot.get('days') or 0),
        'first_at': tot.get('first_at'),
        'last_at': tot.get('last_at'),
        'avg_session_sec': (total_sec / sessions) if sessions else 0,
        'by_hour': by_hour,
        'profile': {'genres': genres, 'actresses': actresses, 'studios': studios,
                    'series': series, 'directors': directors},
        'recent': [dict(r) for r in recent],
        'top_movies': [dict(r) for r in top_movies],
    }


def actress_stats(conn: sqlite3.Connection, aid: int) -> Dict[str, Any]:
    """女优 Rich 档案聚合：数值指标 + 类型/厂商/系列分布 + 代表作。

    通过 movie_actress 桥表聚合该女优出演的全部影片，结果全部来自已有数据，
    不依赖任何外部二进制（如 ffmpeg）。
    """
    row = query_one(
        conn,
        """SELECT MIN(m.year) AS first_year, MAX(m.year) AS last_year,
                  AVG(CASE WHEN m.rating > 0 THEN m.rating END) AS avg_rating,
                  SUM(m.runtime) AS total_runtime,
                  SUM(m.size) AS total_size,
                  SUM(CASE WHEN m.watched THEN 1 ELSE 0 END) AS watched,
                  SUM(CASE WHEN m.favorite THEN 1 ELSE 0 END) AS favorited,
                  SUM(CASE WHEN m.cover <> '' THEN 1 ELSE 0 END) AS with_cover
           FROM movie_actress ma JOIN movies m ON m.id = ma.movie_id
           WHERE ma.actress_id = ?""",
        (aid,),
    ) or {}

    top_genres = query_all(
        conn,
        """SELECT g.name AS name, COUNT(*) AS count
           FROM movie_actress ma
           JOIN movie_genre mg ON mg.movie_id = ma.movie_id
           JOIN genres g ON g.id = mg.genre_id
           WHERE ma.actress_id = ?
           GROUP BY g.id ORDER BY count DESC, g.name LIMIT 12""",
        (aid,),
    )
    top_studios = query_all(
        conn,
        """SELECT s.name AS name, COUNT(*) AS count
           FROM movie_actress ma
           JOIN movies m ON m.id = ma.movie_id
           JOIN studios s ON s.id = m.studio_id
           WHERE ma.actress_id = ? AND m.studio_id IS NOT NULL
           GROUP BY s.id ORDER BY count DESC, s.name LIMIT 12""",
        (aid,),
    )
    top_series = query_all(
        conn,
        """SELECT se.name AS name, COUNT(*) AS count
           FROM movie_actress ma
           JOIN movies m ON m.id = ma.movie_id
           JOIN series se ON se.id = m.series_id
           WHERE ma.actress_id = ? AND m.series_id IS NOT NULL
           GROUP BY se.id ORDER BY count DESC, se.name LIMIT 12""",
        (aid,),
    )
    best = query_all(
        conn,
        """SELECT m.id, m.code, m.title, m.cover, m.rating, m.release_date
           FROM movie_actress ma JOIN movies m ON m.id = ma.movie_id
           WHERE ma.actress_id = ? AND m.rating > 0
           ORDER BY m.rating DESC, m.id DESC LIMIT 4""",
        (aid,),
    )
    return {
        "first_year": row.get("first_year"),
        "last_year": row.get("last_year"),
        "avg_rating": round(float(row.get("avg_rating") or 0), 1),
        "total_runtime": int(row.get("total_runtime") or 0),
        "total_size": int(row.get("total_size") or 0),
        "watched": int(row.get("watched") or 0),
        "favorited": int(row.get("favorited") or 0),
        "with_cover": int(row.get("with_cover") or 0),
        "total": int(scalar(conn, "SELECT COUNT(*) FROM movie_actress WHERE actress_id = ?", (aid,)) or 0),
        "top_genres": [{"name": r["name"], "count": int(r["count"])} for r in top_genres],
        "top_studios": [{"name": r["name"], "count": int(r["count"])} for r in top_studios],
        "top_series": [{"name": r["name"], "count": int(r["count"])} for r in top_series],
        "best": [dict(r) for r in best],
    }


def actress_detail(conn: sqlite3.Connection, ident: Any, page: int = 1,
                    size: int = 24) -> Optional[Dict[str, Any]]:
    """女优详情：基本信息 + 其出演的影片（分页卡片）。ident 可为 id 或名称。"""
    if str(ident).isdigit():
        a = query_one(conn, "SELECT * FROM actresses WHERE id = ?", (int(ident),))
    else:
        a = query_one(conn, "SELECT * FROM actresses WHERE name = ?", (str(ident),))
    if not a:
        return None
    aid = a["id"]
    count = int(scalar(conn, "SELECT COUNT(*) FROM movie_actress WHERE actress_id = ?", (aid,)) or 0)
    sample = query_one(
        conn,
        "SELECT m.id FROM movie_actress ma JOIN movies m ON m.id = ma.movie_id "
        "WHERE ma.actress_id = ? ORDER BY m.id LIMIT 1",
        (aid,),
    )
    info = {k: a.get(k) for k in ("id", "name", "alias", "avatar", "birthday", "note", "favorite")}
    info["count"] = count
    info["sample_id"] = sample["id"] if sample else None
    co = query_all(
        conn,
        """SELECT a.name AS name, COUNT(*) AS c
           FROM movie_actress ma2
           JOIN movie_actress ma1 ON ma1.movie_id = ma2.movie_id
           JOIN actresses a ON a.id = ma2.actress_id
           WHERE ma1.actress_id = ? AND ma2.actress_id <> ?
           GROUP BY a.id ORDER BY c DESC LIMIT 12""",
        (aid, aid),
    )
    info["co_actresses"] = [{"name": r["name"], "count": int(r["c"])} for r in co]
    info["stats"] = actress_stats(conn, aid)
    total = count
    pages = max(1, (total + size - 1) // size)
    page = max(1, min(int(page), pages))
    offset = (page - 1) * size
    rows = query_all(
        conn,
        "SELECT m.* FROM movie_actress ma JOIN movies m ON m.id = ma.movie_id "
        "WHERE ma.actress_id = ? ORDER BY m.release_date DESC, m.id DESC LIMIT ? OFFSET ?",
        (aid, size, offset),
    )
    return {
        "info": info,
        "total": total,
        "page": page,
        "pages": pages,
        "items": [_row_to_card(r) for r in rows],
    }


def movie_detail(conn: sqlite3.Connection, movie_id: int) -> Optional[Dict[str, Any]]:
    row = query_one(
        conn,
        """SELECT m.*, st.name AS studio, pb.name AS publisher, se.name AS series
           FROM movies m
           LEFT JOIN studios st ON st.id = m.studio_id
           LEFT JOIN studios pb ON pb.id = m.publisher_id
           LEFT JOIN series  se ON se.id = m.series_id
           WHERE m.id = ?""",
        (movie_id,),
    )
    if not row:
        return None
    row["studio"] = row.get("studio") or ""
    row["publisher"] = row.get("publisher") or ""
    row["series"] = row.get("series") or ""
    row["display_code"] = row["code"] if row["has_code"] else ""
    row["actresses"] = get_relations(conn, movie_id, "actress")
    row["genres"] = get_relations(conn, movie_id, "genre")
    row["tags"] = get_relations(conn, movie_id, "tag")
    row["files"] = query_all(
        conn,
        "SELECT id, path, filename, ext, size, mtime, part, missing FROM movie_files "
        "WHERE movie_id = ? ORDER BY part, filename",
        (movie_id,),
    )
    row["progress"] = movie_progress(conn, movie_id)
    row["subtitles"] = subtitles.list_subtitles(conn, movie_id)
    return row


_EDITABLE = {
    "title", "original_title", "plot", "release_date", "runtime", "director",
    "rating", "favorite", "watched", "watchlist", "note", "code", "subtitle", "uncensored",
    "leak", "hd4k", "vr",
}


def update_movie(conn: sqlite3.Connection, movie_id: int, payload: Dict[str, Any]) -> None:
    sets: List[str] = []
    args: List[Any] = []

    for field in _EDITABLE:
        if field in payload:
            value = payload[field]
            if field in {"runtime", "favorite", "watched", "watchlist", "subtitle", "uncensored", "leak", "hd4k", "vr"}:
                value = int(value or 0)
            elif field == "rating":
                value = float(value or 0)
            else:
                value = norm_name(value) if field != "plot" else str(value or "")
            sets.append(f"{field} = ?")
            args.append(value)

    if "release_date" in payload:
        m = re.search(r"(\d{4})", str(payload.get("release_date") or ""))
        sets.append("year = ?")
        args.append(int(m.group(1)) if m else None)

    # 人工补填番号后，应当同步标记为「已识别」
    if "code" in payload:
        sets.append("has_code = ?")
        args.append(1 if norm_name(payload["code"]) else 0)

    for field, table in (("studio", "studios"), ("publisher", "studios"), ("series", "series")):
        if field in payload:
            col = "series_id" if field == "series" else f"{field}_id"
            sets.append(f"{col} = ?")
            args.append(get_or_create(conn, table, payload[field]))

    if sets:
        sets.append("updated_at = datetime('now','localtime')")
        conn.execute(f"UPDATE movies SET {', '.join(sets)} WHERE id = ?", [*args, movie_id])

    for kind, field in (("actress", "actresses"), ("genre", "genres"), ("tag", "tags")):
        if field in payload:
            set_relations(conn, movie_id, kind, _split_multi(payload[field]))


def delete_movie(conn: sqlite3.Connection, movie_id: int) -> None:
    conn.execute("DELETE FROM movies WHERE id = ?", (movie_id,))


def cleanup_orphans(conn: sqlite3.Connection) -> Dict[str, int]:
    """清理没有任何关联影片的女优/类型/厂商/系列。"""
    result = {}
    result["actresses"] = conn.execute(
        "DELETE FROM actresses WHERE id NOT IN (SELECT actress_id FROM movie_actress)").rowcount
    result["genres"] = conn.execute(
        "DELETE FROM genres WHERE id NOT IN (SELECT genre_id FROM movie_genre)").rowcount
    result["tags"] = conn.execute(
        "DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM movie_tag)").rowcount
    result["studios"] = conn.execute(
        "DELETE FROM studios WHERE id NOT IN (SELECT COALESCE(studio_id,-1) FROM movies "
        "UNION SELECT COALESCE(publisher_id,-1) FROM movies)").rowcount
    result["series"] = conn.execute(
        "DELETE FROM series WHERE id NOT IN (SELECT COALESCE(series_id,-1) FROM movies)").rowcount
    return result


# ----------------------------------------------------------------- 发现与片单


def movie_progress(conn: sqlite3.Connection, movie_id: int) -> Optional[Dict[str, Any]]:
    row = query_one(
        conn, "SELECT position, duration, finished FROM watch_progress WHERE movie_id = ?", (movie_id,))
    if not row:
        return None
    return {"position": row["position"], "duration": row["duration"], "finished": row["finished"]}


def mark_played(conn: sqlite3.Connection, movie_id: int) -> None:
    """网页播放器开始播放时调用：播放次数 +1，并标记已看、更新最后播放时间。
    不会打开系统播放器，也不会启动外部监控。"""
    conn.execute(
        "UPDATE movies SET play_count = play_count + 1, watched = 1, "
        "last_played = datetime('now','localtime') WHERE id = ?",
        (movie_id,),
    )


def set_watch_progress(conn: sqlite3.Connection, movie_id: int,
                       position: float = 0, duration: float = 0) -> None:
    """记录/更新播放进度（秒）。position=0 视为清除进度；看过即标记 watched。"""
    position = float(position or 0)
    duration = float(duration or 0)
    finished = 1 if (duration and position >= duration * 0.95) else 0
    conn.execute(
        """INSERT INTO watch_progress(movie_id, position, duration, finished, updated_at)
           VALUES(?,?,?,?,datetime('now','localtime'))
           ON CONFLICT(movie_id) DO UPDATE SET
             position=excluded.position, duration=excluded.duration,
             finished=excluded.finished, updated_at=datetime('now','localtime')""",
        (movie_id, position, duration, finished),
    )
    if position > 0:
        conn.execute(
            "UPDATE movies SET watched = 1, last_played = datetime('now','localtime') WHERE id = ?",
            (movie_id,),
        )
    else:
        conn.execute("UPDATE movies SET watched = 0 WHERE id = ?", (movie_id,))


def continue_watching(conn: sqlite3.Connection, limit: int = 20) -> Dict[str, Any]:
    """返回「还没看完」的影片（有进度且未标记为看完），按最近观看排序。"""
    ids = [r["movie_id"] for r in query_all(
        conn,
        "SELECT movie_id FROM watch_progress WHERE position > 0 AND finished = 0 "
        "ORDER BY updated_at DESC LIMIT ?", (limit,))]
    if not ids:
        return {"items": [], "total": 0}
    placeholders = ",".join("?" * len(ids))
    rows = query_all(conn, f"{_LIST_SELECT} WHERE m.id IN ({placeholders})", ids)
    prog = {r["movie_id"]: r for r in query_all(
        conn,
        f"SELECT movie_id, position, duration FROM watch_progress WHERE movie_id IN ({placeholders})",
        ids)}
    order = {mid: i for i, mid in enumerate(ids)}
    items = []
    for r in rows:
        card = _row_to_card(r)
        p = prog.get(card["id"], {})
        pos = p.get("position") or 0
        dur = p.get("duration") or 0
        card["progress"] = {
            "position": pos,
            "duration": dur,
            "percent": round(pos / dur * 100, 1) if dur else 0,
        }
        items.append(card)
    items.sort(key=lambda c: order.get(c["id"], 999))
    return {"items": items, "total": len(items)}


def similar_movies(conn: sqlite3.Connection, movie_id: int, limit: int = 12) -> Dict[str, Any]:
    """基于共演女优(权重10) / 同类型(4) / 同厂商(3) / 同系列(3) / 同导演(2) 的相似度推荐。

    兜底增强：即使影片未刮削（无 series_id / 女优等），只要番号可识别，仍可凭「番号前缀
    即系列代码」(如 F2C / SSIS) 归入同系列，从而参与相似推荐——命名不同(编号不同)的同一
    系列影片因此也能被推荐出来。
    """
    # 目标影片的番号前缀（大写，取 '-' 之前的部分），无番号则为空串
    tgt_code = query_one(conn, "SELECT code FROM movies WHERE id = ?", (movie_id,))
    tgt_prefix = ""
    if tgt_code and tgt_code["code"]:
        c = tgt_code["code"].upper()
        pos = c.find("-")
        tgt_prefix = c[:pos] if pos > 0 else c
    has_prefix = tgt_prefix != ""

    score_sql = """
        WITH scored AS (
            SELECT m.id AS mid, m.created_at AS cdate,
              (SELECT COUNT(*) FROM movie_actress ma2
                  JOIN movie_actress ma_t ON ma_t.actress_id = ma2.actress_id
                  WHERE ma_t.movie_id = ? AND ma2.movie_id = m.id) * 10
              + (SELECT COUNT(*) FROM movie_genre mg2
                  JOIN movie_genre mg_t ON mg_t.genre_id = mg2.genre_id
                  WHERE mg_t.movie_id = ? AND mg2.movie_id = m.id) * 4
              + (CASE WHEN m.studio_id IS NOT NULL AND m.studio_id = (SELECT studio_id FROM movies WHERE id = ?) THEN 3 ELSE 0 END)
              + (CASE
                   WHEN m.series_id IS NOT NULL AND m.series_id = (SELECT series_id FROM movies WHERE id = ?) THEN 3
                   WHEN ? = 1 AND m.has_code = 1 THEN
                     (CASE WHEN substr(UPPER(m.code), 1, CASE WHEN instr(UPPER(m.code), '-') > 1 THEN instr(UPPER(m.code), '-') - 1 ELSE length(m.code) END) = ? THEN 3 ELSE 0 END)
                   ELSE 0 END)
              + (CASE WHEN m.director <> '' AND m.director = (SELECT director FROM movies WHERE id = ?) THEN 2 ELSE 0 END) AS score
            FROM movies m
            WHERE m.id <> ?
        )
        SELECT mid, score FROM scored WHERE score > 0 ORDER BY score DESC, cdate DESC LIMIT ?
    """
    # 参数顺序: 女优?, 类型?, studio?, series?, has_prefix?, prefix?, 导演?, id, limit
    rows = query_all(conn, score_sql,
                     [movie_id, movie_id, movie_id, movie_id, (1 if has_prefix else 0), tgt_prefix, movie_id, movie_id, limit])
    ids = [r["mid"] for r in rows]
    if not ids:
        return {"items": [], "total": 0}
    placeholders = ",".join("?" * len(ids))
    cards = {c["id"]: c for c in (
        _row_to_card(r) for r in query_all(conn, f"{_LIST_SELECT} WHERE m.id IN ({placeholders})", ids))}
    ordered = [cards[i] for i in ids if i in cards]
    return {"items": ordered, "total": len(ordered)}


# ----- 用户自建片单 -----


def list_collections(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = query_all(
        conn,
        """SELECT c.id, c.name, c.kind, c.rule, c.cover_movie_id,
                  (SELECT COUNT(*) FROM collection_items ci WHERE ci.collection_id = c.id) AS count,
                  (SELECT m.cover FROM collection_items ci JOIN movies m ON m.id = ci.movie_id
                      WHERE ci.collection_id = c.id AND m.cover <> '' LIMIT 1) AS cover,
                  (SELECT m.id FROM collection_items ci JOIN movies m ON m.id = ci.movie_id
                      WHERE ci.collection_id = c.id AND m.cover <> '' LIMIT 1) AS cover_id
           FROM collections c ORDER BY c.created_at DESC, c.id DESC""",
    )
    return [dict(r) for r in rows]


def create_collection(conn: sqlite3.Connection, name: str, kind: str = "manual", rule: Any = "") -> int:
    name = norm_name(name)
    if not name:
        raise ValueError("片单名称不能为空")
    if isinstance(rule, (dict, list)):
        rule = _j(rule)
    return int(conn.execute(
        "INSERT INTO collections(name, kind, rule) VALUES(?,?,?)", (name, kind, rule or "")).lastrowid)


def rename_collection(conn: sqlite3.Connection, cid: int, name: str) -> None:
    name = norm_name(name)
    if not name:
        raise ValueError("片单名称不能为空")
    conn.execute("UPDATE collections SET name = ? WHERE id = ?", (name, cid))


def delete_collection(conn: sqlite3.Connection, cid: int) -> None:
    conn.execute("DELETE FROM collections WHERE id = ?", (cid,))


def add_to_collection(conn: sqlite3.Connection, cid: int, movie_id: int) -> None:
    info = conn.execute("SELECT kind FROM collections WHERE id=?", (cid,)).fetchone()
    if info and (info["kind"] or "manual") == "smart":
        raise ValueError("智能清单由规则自动生成，不能手动添加")
    conn.execute(
        "INSERT OR IGNORE INTO collection_items(collection_id, movie_id, position) "
        "SELECT ?, ?, COALESCE(MAX(position), 0) + 1 FROM collection_items WHERE collection_id = ?",
        (cid, movie_id, cid))


def remove_from_collection(conn: sqlite3.Connection, cid: int, movie_id: int) -> None:
    conn.execute(
        "DELETE FROM collection_items WHERE collection_id = ? AND movie_id = ?", (cid, movie_id))


def smart_query(conn: sqlite3.Connection, rule: Any, page: int = 1,
                 size: int = 60) -> Dict[str, Any]:
    """按智能清单规则聚合影片。

    rule 可以是 JSON 字符串（库里存储形态）或 dict；约定取 rule["params"] 作为
    筛选参数。参数键与 search_movies 基本一致：cond 为状态标志（watched/unwatched/
    favorite 等，见 FLAG_CLAUSES）、min_rating 评分下限、genre/actress 分类名、
    sort 排序键。"""
    params: Dict[str, Any] = {}
    if isinstance(rule, str):
        try:
            rule = json.loads(rule)
        except Exception:
            rule = {}
    if isinstance(rule, dict):
        params = rule.get("params") if isinstance(rule.get("params"), dict) else dict(rule)
    params = params or {}

    # cond 是前端单选的状态条件（如 unwatched/watched/favorite），映射到 FLAG_CLAUSES
    cond = params.get("cond")
    if cond and cond not in params:
        params[cond] = 1

    where, args = _build_where(params)
    order = SORTS.get(str(params.get("sort") or "added_desc"), SORTS["added_desc"])
    page = max(1, int(page or 1))
    size = min(500, max(1, int(size or 60)))
    offset = (page - 1) * size

    total = int(scalar(
        conn,
        "SELECT COUNT(*) FROM movies m "
        "LEFT JOIN studios st ON st.id = m.studio_id "
        "LEFT JOIN series se ON se.id = m.series_id" + where,
        args,
    ))
    rows = query_all(
        conn,
        f"{_LIST_SELECT}{where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*args, size, offset],
    )
    return {
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, (total + size - 1) // size),
        "items": [_row_to_card(r) for r in rows],
    }


def collection_movies(conn: sqlite3.Connection, cid: int, page: int = 1,
                      size: int = 60) -> Dict[str, Any]:
    info = conn.execute("SELECT kind, rule FROM collections WHERE id=?", (cid,)).fetchone()
    if info and (info["kind"] or "manual") == "smart":
        return smart_query(conn, info["rule"], page, size)
    total = int(scalar(conn, "SELECT COUNT(*) FROM collection_items WHERE collection_id = ?", (cid,)))
    pages = max(1, (total + size - 1) // size)
    page = max(1, min(int(page), pages))
    offset = (page - 1) * size
    rows = query_all(
        conn,
        f"{_LIST_SELECT} JOIN collection_items ci ON ci.movie_id = m.id "
        "WHERE ci.collection_id = ? ORDER BY ci.position ASC, m.id DESC LIMIT ? OFFSET ?",
        (cid, size, offset),
    )
    return {
        "total": total,
        "page": page,
        "page_size": size,
        "pages": pages,
        "items": [_row_to_card(r) for r in rows],
    }


# ----------------------------------------------------------------- 聚合面板


def facets(conn: sqlite3.Connection, limit: int = 300) -> Dict[str, Any]:
    def top(sql: str) -> List[Dict[str, Any]]:
        return query_all(conn, sql, (limit,))

    return {
        "actresses": top(
            "SELECT a.name, COUNT(*) AS count FROM movie_actress ma "
            "JOIN actresses a ON a.id = ma.actress_id GROUP BY a.id "
            "ORDER BY count DESC, a.name LIMIT ?"),
        "genres": top(
            "SELECT g.name, COUNT(*) AS count FROM movie_genre mg "
            "JOIN genres g ON g.id = mg.genre_id GROUP BY g.id "
            "ORDER BY count DESC, g.name LIMIT ?"),
        "studios": top(
            "SELECT st.name, COUNT(*) AS count FROM movies m "
            "JOIN studios st ON st.id = m.studio_id GROUP BY st.id "
            "ORDER BY count DESC, st.name LIMIT ?"),
        "series": top(
            "SELECT se.name, COUNT(*) AS count FROM movies m "
            "JOIN series se ON se.id = m.series_id GROUP BY se.id "
            "ORDER BY count DESC, se.name LIMIT ?"),
        "tags": top(
            "SELECT t.name, COUNT(*) AS count FROM movie_tag mt "
            "JOIN tags t ON t.id = mt.tag_id GROUP BY t.id "
            "ORDER BY count DESC, t.name LIMIT ?"),
        "years": query_all(
            conn,
            "SELECT year AS name, year, COUNT(*) AS count FROM movies WHERE year IS NOT NULL "
            "GROUP BY year ORDER BY year DESC"),
        "prefixes": query_all(
            conn,
            "SELECT substr(code, 1, instr(code, '-') - 1) AS name, COUNT(*) AS count "
            "FROM movies WHERE has_code = 1 AND instr(code, '-') > 1 "
            "GROUP BY name ORDER BY count DESC, name LIMIT ?", (limit,)),
    }


def actress_wall(conn: sqlite3.Connection, q: str = "", sort: str = "count",
                 limit: int = 500, followed_only: bool = False) -> List[Dict[str, Any]]:
    order = {"count": "count DESC, a.name", "name": "a.name", "recent": "last_add DESC", "followed": "a.followed DESC, count DESC"}.get(sort, "count DESC")
    where, args = ("WHERE a.name LIKE ?", [f"%{norm_name(q)}%"]) if norm_name(q) else ("", [])
    if followed_only:
        where = where + " AND a.followed=1" if where else "WHERE a.followed=1"
    return query_all(
        conn,
        f"""SELECT a.id, a.name, a.avatar, a.favorite, a.followed, COUNT(ma.movie_id) AS count,
                   MAX(m.created_at) AS last_add,
                   (SELECT m2.cover FROM movie_actress ma2 JOIN movies m2 ON m2.id = ma2.movie_id
                     WHERE ma2.actress_id = a.id AND m2.cover <> '' LIMIT 1) AS sample_cover,
                   (SELECT m3.id FROM movie_actress ma3 JOIN movies m3 ON m3.id = ma3.movie_id
                     WHERE ma3.actress_id = a.id AND m3.cover <> '' LIMIT 1) AS sample_id
            FROM actresses a
            LEFT JOIN movie_actress ma ON ma.actress_id = a.id
            LEFT JOIN movies m ON m.id = ma.movie_id
            {where}
            GROUP BY a.id
            ORDER BY {order}
            LIMIT ?""",
        [*args, limit],
    )


def storage_stats(conn):
    """磁盘占用分布：按盘符、厂商、年份、文件类型，最大文件，以及整体汇总。"""
    by_disk = conn.execute(
        "SELECT substr(f.path, 1, 3) AS drive, COUNT(*) AS files, "
        "SUM(f.size) AS bytes, COUNT(DISTINCT f.movie_id) AS movies "
        "FROM movie_files f WHERE f.missing = 0 GROUP BY drive ORDER BY bytes DESC"
    ).fetchall()
    by_studio = conn.execute(
        "SELECT COALESCE(s.name, '未知') AS studio, "
        "COUNT(*) AS n, SUM(f.size) AS bytes "
        "FROM movies m JOIN movie_files f ON f.movie_id = m.id AND f.missing = 0 "
        "LEFT JOIN studios s ON s.id = m.studio_id "
        "GROUP BY studio ORDER BY bytes DESC LIMIT 15"
    ).fetchall()
    by_year = conn.execute(
        "SELECT COALESCE(m.year, 0) AS year, COUNT(*) AS n, SUM(f.size) AS bytes "
        "FROM movies m JOIN movie_files f ON f.movie_id = m.id AND f.missing = 0 "
        "GROUP BY year ORDER BY year DESC LIMIT 15"
    ).fetchall()
    by_ext = conn.execute(
        "SELECT COALESCE(NULLIF(f.ext, ''), '未知') AS ext, "
        "COUNT(*) AS files, SUM(f.size) AS bytes "
        "FROM movie_files f WHERE f.missing = 0 GROUP BY ext ORDER BY bytes DESC"
    ).fetchall()
    by_genre = conn.execute(
        "SELECT COALESCE(g.name, '未分类') AS genre, "
        "COUNT(DISTINCT m.id) AS movies, SUM(f.size) AS bytes "
        "FROM movies m "
        "JOIN movie_files f ON f.movie_id = m.id AND f.missing = 0 "
        "LEFT JOIN movie_genre mg ON mg.movie_id = m.id "
        "LEFT JOIN genres g ON g.id = mg.genre_id "
        "GROUP BY g.id ORDER BY bytes DESC LIMIT 15"
    ).fetchall()
    largest = conn.execute(
        "SELECT f.id AS file_id, f.size AS bytes, f.filename, f.ext, f.movie_id, "
        "COALESCE(NULLIF(m.title, ''), m.code, '#' || m.id) AS movie_name "
        "FROM movie_files f JOIN movies m ON m.id = f.movie_id "
        "WHERE f.missing = 0 ORDER BY f.size DESC LIMIT 8"
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) AS movies, SUM(file_count) AS files, SUM(size) AS bytes FROM movies"
    ).fetchone()
    return {
        "by_disk": [dict(r) for r in by_disk],
        "by_studio": [dict(r) for r in by_studio],
        "by_year": [dict(r) for r in by_year],
        "by_ext": [dict(r) for r in by_ext],
        "by_genre": [dict(r) for r in by_genre],
        "largest": [dict(r) for r in largest],
        "total": dict(total),
    }


def integrity_issues(conn):
    """完整性概览：缺失文件、缺封面、未识别番号。"""
    missing = conn.execute("SELECT COUNT(*) AS c FROM movie_files WHERE missing = 1").fetchone()["c"]
    no_cover = conn.execute(
        "SELECT COUNT(*) AS c FROM movies m WHERE (m.cover IS NULL OR m.cover = '') "
        "AND EXISTS(SELECT 1 FROM movie_files f WHERE f.movie_id = m.id AND f.missing = 0)"
    ).fetchone()["c"]
    unrecognized = conn.execute(
        "SELECT COUNT(*) AS c FROM movies m WHERE m.has_code = 0 "
        "AND EXISTS(SELECT 1 FROM movie_files f WHERE f.movie_id = m.id AND f.missing = 0)"
    ).fetchone()["c"]
    return {"missing_files": missing, "missing_cover": no_cover, "unrecognized": unrecognized}


def batch_update(conn, movie_ids, payload):
    """批量更新：收藏/评分/已看标记，以及标签并集替换。返回影响影片数。"""
    updated = 0
    tags = payload.get("tags")
    for mid in movie_ids:
        sets, args = [], []
        if "favorite" in payload:
            sets.append("favorite = ?"); args.append(int(payload["favorite"]))
        if "rating" in payload:
            sets.append("rating = ?"); args.append(int(payload["rating"]))
        if "watched" in payload:
            sets.append("watched = ?"); args.append(int(payload["watched"]))
        if "watchlist" in payload:
            sets.append("watchlist = ?"); args.append(int(payload["watchlist"]))
        if sets:
            args.append(mid)
            conn.execute("UPDATE movies SET " + ", ".join(sets) + " WHERE id = ?", args)
            updated += 1
        if tags is not None:
            conn.execute("DELETE FROM movie_tag WHERE movie_id = ?", (mid,))
            for t in tags:
                tid = ensure_tag(conn, t)
                conn.execute("INSERT OR IGNORE INTO movie_tag(movie_id, tag_id) VALUES(?,?)", (mid, tid))
            updated += 1
        genres = payload.get("genres")
        if genres is not None:
            cur = set(get_relations(conn, mid, "genre"))
            for g in genres:
                cur.add(g)
            set_relations(conn, mid, "genre", cur, replace=True)
            updated += 1
        actresses = payload.get("actresses")
        if actresses is not None:
            cur = set(get_relations(conn, mid, "actress"))
            for a in actresses:
                cur.add(a)
            set_relations(conn, mid, "actress", cur, replace=True)
            updated += 1
    conn.commit()
    return updated


def stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    total = int(scalar(conn, "SELECT COUNT(*) FROM movies"))
    return {
        "movies": total,
        "files": int(scalar(conn, "SELECT COUNT(*) FROM movie_files")),
        "size": int(scalar(conn, "SELECT COALESCE(SUM(size),0) FROM movies")),
        "runtime": int(scalar(conn, "SELECT COALESCE(SUM(runtime),0) FROM movies")),
        "actresses": int(scalar(conn, "SELECT COUNT(*) FROM actresses")),
        "genres": int(scalar(conn, "SELECT COUNT(*) FROM genres")),
        "studios": int(scalar(conn, "SELECT COUNT(*) FROM studios")),
        "series": int(scalar(conn, "SELECT COUNT(*) FROM series")),
        "with_cover": int(scalar(conn, "SELECT COUNT(*) FROM movies WHERE cover <> ''")),
        "scraped": int(scalar(conn, "SELECT COUNT(*) FROM movies WHERE scraped_at <> ''")),
        "no_code": int(scalar(conn, "SELECT COUNT(*) FROM movies WHERE has_code = 0")),
        "subtitle": int(scalar(conn, "SELECT COUNT(*) FROM movies WHERE subtitle = 1")),
        "uncensored": int(scalar(conn, "SELECT COUNT(*) FROM movies WHERE uncensored = 1")),
        "favorite": int(scalar(conn, "SELECT COUNT(*) FROM movies WHERE favorite = 1")),
        "watched": int(scalar(conn, "SELECT COUNT(*) FROM movies WHERE watched = 1")),
        "watchlist": int(scalar(conn, "SELECT COUNT(*) FROM movies WHERE watchlist = 1")),
        "top_actresses": query_all(
            conn,
            "SELECT a.name, COUNT(*) AS count FROM movie_actress ma "
            "JOIN actresses a ON a.id = ma.actress_id GROUP BY a.id ORDER BY count DESC LIMIT 12"),
        "top_studios": query_all(
            conn,
            "SELECT st.name, COUNT(*) AS count FROM movies m JOIN studios st ON st.id = m.studio_id "
            "GROUP BY st.id ORDER BY count DESC LIMIT 12"),
        "top_genres": query_all(
            conn,
            "SELECT g.name, COUNT(*) AS count FROM movie_genre mg "
            "JOIN genres g ON g.id = mg.genre_id GROUP BY g.id ORDER BY count DESC LIMIT 16"),
        "top_series": query_all(
            conn,
            "SELECT se.name, COUNT(*) AS count FROM movies m "
            "JOIN series se ON se.id = m.series_id GROUP BY se.id ORDER BY count DESC LIMIT 12"),
        "by_year": query_all(
            conn,
            "SELECT year, COUNT(*) AS count FROM movies WHERE year IS NOT NULL "
            "GROUP BY year ORDER BY year"),
        "recent": query_all(
            conn,
            f"{_LIST_SELECT} ORDER BY m.created_at DESC, m.id DESC LIMIT 12"),
    }


# ----------------------------------------------------------------- 关注女优
def toggle_follow(conn: sqlite3.Connection, actress_id: int) -> int:
    cur = conn.execute("SELECT followed FROM actresses WHERE id=?", (actress_id,)).fetchone()
    if not cur:
        return 0
    nv = 0 if cur[0] else 1
    conn.execute("UPDATE actresses SET followed=? WHERE id=?", (nv, actress_id))
    return nv


# ----------------------------------------------------------------- 排行榜
def rankings(conn: sqlite3.Connection, kind: str = "watched", limit: int = 30) -> List[Dict[str, Any]]:
    kind = str(kind or "watched")
    if kind == "watched":
        sel = _LIST_SELECT.replace(
            "FROM movies m",
            ", (SELECT COALESCE(SUM(watched_sec),0) FROM watch_sessions ws WHERE ws.movie_id=m.id) AS watched_sec "
            "FROM movies m",
            1,
        )
        rows = query_all(conn, f"{sel} ORDER BY watched_sec DESC, m.rating DESC LIMIT ?", (limit,))
    elif kind == "rating":
        rows = query_all(conn, f"{_LIST_SELECT} WHERE m.rating>0 ORDER BY m.rating DESC, m.play_count DESC LIMIT ?", (limit,))
    elif kind == "favorite":
        rows = query_all(conn, f"{_LIST_SELECT} WHERE m.favorite=1 ORDER BY m.rating DESC, m.play_count DESC LIMIT ?", (limit,))
    else:  # play
        rows = query_all(conn, f"{_LIST_SELECT} ORDER BY m.play_count DESC, m.rating DESC LIMIT ?", (limit,))
    return [_row_to_card(r) for r in rows]


def watch_history(conn: sqlite3.Connection, page: int = 1,
                  size: int = 50) -> Dict[str, Any]:
    """按时间倒序返回每次观看的明细（一条观看记录一行）。"""
    page = max(1, int(page or 1))
    size = min(200, max(1, int(size or 50)))
    offset = (page - 1) * size
    total = int(scalar(conn, "SELECT COUNT(*) FROM watch_sessions") or 0)
    rows = query_all(conn, """
        SELECT s.id, s.movie_id, s.started_at, s.ended_at, s.watched_sec,
               s.finished, s.method, s.start_pos, s.end_pos,
               m.code, m.title, m.cover, m.year, st.name AS studio
        FROM watch_sessions s
        JOIN movies m ON m.id = s.movie_id
        LEFT JOIN studios st ON st.id = m.studio_id
        ORDER BY s.started_at DESC, s.id DESC LIMIT ? OFFSET ?""", (size, offset))
    items = [{
        "id": r["id"],
        "movie_id": r["movie_id"],
        "code": r["code"],
        "title": r["title"],
        "cover": r["cover"],
        "studio": r["studio"],
        "year": r["year"],
        "started_at": r["started_at"],
        "ended_at": r["ended_at"],
        "watched_sec": float(r["watched_sec"] or 0),
        "finished": int(r["finished"] or 0),
        "method": r["method"] or "external",
        "start_pos": float(r["start_pos"] or 0),
        "end_pos": float(r["end_pos"] or 0),
    } for r in rows]
    return {
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, (total + size - 1) // size),
        "items": items,
    }


# ----------------------------------------------------------------- 文件体检（明细）
def health_check(conn: sqlite3.Connection) -> Dict[str, Any]:
    missing = query_all(
        conn,
        "SELECT f.movie_id, m.code, m.title, f.path, f.part "
        "FROM movie_files f LEFT JOIN movies m ON m.id=f.movie_id WHERE f.missing=1")
    no_cover = query_all(
        conn,
        "SELECT m.id, m.code, m.title FROM movies m "
        "WHERE (m.cover IS NULL OR m.cover='') "
        "AND EXISTS(SELECT 1 FROM movie_files f WHERE f.movie_id=m.id AND f.missing=0)")
    unrecognized = query_all(
        conn,
        "SELECT m.id, m.code, m.title, m.folder FROM movies m "
        "WHERE m.has_code=0 AND EXISTS(SELECT 1 FROM movie_files f WHERE f.movie_id=m.id AND f.missing=0)")
    duplicates = query_all(
        conn,
        "SELECT f1.movie_id AS a, f2.movie_id AS b, f1.size, f1.path AS p1, f2.path AS p2 "
        "FROM movie_files f1 JOIN movie_files f2 ON f1.size=f2.size AND f1.id<f2.id "
        "AND f1.size>0 AND f1.missing=0 AND f2.missing=0 LIMIT 200")
    # 分片不完整：有 part=1 但同片无 part=2 的多文件影片
    split_incomplete = query_all(
        conn,
        "SELECT m.id, m.code, m.title, COUNT(*) AS parts FROM movies m "
        "JOIN movie_files f ON f.movie_id=m.id AND f.missing=0 "
        "WHERE m.file_count>1 GROUP BY m.id HAVING MAX(f.part) < m.file_count")
    # 占位图封面：文件存在但内容哈希命中已知占位图（如 DMM jppl.jpg）
    placeholder = query_all(
        conn,
        "SELECT m.id, m.code, m.title, m.cover FROM movies m "
        "WHERE m.cover <> '' "
        "AND EXISTS(SELECT 1 FROM movie_files f WHERE f.movie_id=m.id AND f.missing=0)")
    placeholder = [dict(r) for r in placeholder if cover_is_placeholder(r["cover"])]
    return {
        "missing_files": [dict(r) for r in missing],
        "missing_cover": [dict(r) for r in no_cover],
        "placeholder_cover": placeholder,
        "unrecognized": [dict(r) for r in unrecognized],
        "duplicates": [dict(r) for r in duplicates],
        "split_incomplete": [dict(r) for r in split_incomplete],
        "counts": {
            "missing_files": len(missing),
            "missing_cover": len(no_cover),
            "placeholder_cover": len(placeholder),
            "unrecognized": len(unrecognized),
            "duplicates": len(duplicates),
            "split_incomplete": len(split_incomplete),
        },
    }


# ----------------------------------------------------------------- 存量番号重解析
_PLACEHOLDER_HASHES = {
    # DMM 无封面占位图 jppl.jpg 等已知无效封面
    "8c6455760bf9c0c487142280fcef1877",
}


def cover_is_placeholder(cover_file: str) -> bool:
    """判断封面文件是否为已知占位图（内容哈希命中）。"""
    if not cover_file:
        return False
    from .config import COVER_DIR
    import hashlib
    p = COVER_DIR / cover_file
    if not p.exists():
        return False
    try:
        h = hashlib.md5(p.read_bytes()).hexdigest()
    except Exception:
        return False
    return h in _PLACEHOLDER_HASHES


def reparse_all_codes(conn: sqlite3.Connection, only_missing: bool = True) -> Dict[str, Any]:
    """存量番号重解析：用最新 parser 重新识别影片番号。

    only_missing=True 只处理 has_code=0 的影片；False 则全部重解析（谨慎）。
    返回重解析结果统计。
    """
    from . import parser as _parser
    if only_missing:
        rows = query_all(conn, "SELECT id, title, folder, key FROM movies WHERE has_code = 0")
    else:
        rows = query_all(conn, "SELECT id, title, folder, key FROM movies")
    fixed, failed, unchanged = 0, 0, 0
    for r in rows:
        # 优先用文件夹名，其次标题，最后 key
        candidates = [r["folder"], r["title"], r["key"]]
        new_code = ""
        for cand in candidates:
            parsed = _parser.extract_code(str(cand or ""))
            code = parsed[0] if parsed else ""
            if code:
                new_code, used_rule = code, parsed[1] if len(parsed) > 1 else "reparse"
                break
        if new_code:
            conn.execute(
                "UPDATE movies SET code = ?, has_code = 1, code_rule = ? WHERE id = ?",
                (new_code, used_rule, r["id"]),
            )
            fixed += 1
        else:
            failed += 1
    conn.commit()
    return {"total": len(rows), "fixed": fixed, "failed": failed, "unchanged": unchanged}


# ----------------------------------------------------------------- 统计可视化增强
def stats_enhanced(conn: sqlite3.Connection) -> Dict[str, Any]:
    tag_cloud = query_all(
        conn,
        "SELECT t.name, COUNT(*) AS count FROM movie_tag mt JOIN tags t ON t.id=mt.tag_id "
        "GROUP BY t.id ORDER BY count DESC LIMIT 80")
    runtime_by_year = query_all(
        conn,
        "SELECT COALESCE(year,0) AS year, COUNT(*) AS count, "
        "SUM(COALESCE(runtime,0)) AS minutes FROM movies WHERE runtime>0 GROUP BY year ORDER BY year")
    watch_calendar = query_all(
        conn,
        "SELECT substr(started_at,1,10) AS day, COUNT(*) AS sessions, "
        "SUM(watched_sec) AS sec FROM watch_sessions GROUP BY day ORDER BY day")
    return {
        "tag_cloud": [dict(r) for r in tag_cloud],
        "runtime_by_year": [dict(r) for r in runtime_by_year],
        "watch_calendar": [dict(r) for r in watch_calendar],
    }


# ----------------------------------------------------------------- 标签字典（已有标签）
def list_tags(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """返回所有已存在的标签及其使用次数，用于详情页标签输入建议。"""
    rows = conn.execute(
        "SELECT t.id AS id, t.name AS name, COUNT(mt.movie_id) AS count "
        "FROM tags t LEFT JOIN movie_tag mt ON mt.tag_id = t.id "
        "GROUP BY t.id ORDER BY count DESC, t.name"
    ).fetchall()
    return [{"id": r["id"], "name": r["name"], "count": r["count"]} for r in rows]


def rename_tag(conn: sqlite3.Connection, old_name: str, new_name: str) -> Dict[str, Any]:
    """重命名标签；若新名称已存在则合并（旧标签关联并入新标签并删除旧条目）。"""
    old_name = (old_name or "").strip()
    new_name = (new_name or "").strip()
    if not old_name or not new_name:
        raise ValueError("标签名不能为空")
    if old_name == new_name:
        return {"ok": True, "merged": False}
    old = conn.execute("SELECT id FROM tags WHERE name=?", (old_name,)).fetchone()
    if not old:
        raise ValueError("原标签不存在")
    old_id = old["id"]
    new = conn.execute("SELECT id FROM tags WHERE name=?", (new_name,)).fetchone()
    if new:
        new_id = new["id"]
        # 合并：旧标签关联改指新标签（去重）
        conn.execute(
            "UPDATE OR IGNORE movie_tag SET tag_id=? WHERE tag_id=? "
            "AND movie_id NOT IN (SELECT movie_id FROM movie_tag WHERE tag_id=?)",
            (new_id, old_id, new_id),
        )
        conn.execute("DELETE FROM movie_tag WHERE tag_id=?", (old_id,))
        conn.execute("DELETE FROM tags WHERE id=?", (old_id,))
        return {"ok": True, "merged": True, "into": new_name}
    conn.execute("UPDATE tags SET name=? WHERE id=?", (new_name, old_id))
    return {"ok": True, "merged": False}


def delete_tag(conn: sqlite3.Connection, name: str) -> Dict[str, Any]:
    """删除标签及其全部影片关联。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("标签名不能为空")
    row = conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
    if not row:
        raise ValueError("标签不存在")
    tid = row["id"]
    conn.execute("DELETE FROM movie_tag WHERE tag_id=?", (tid,))
    conn.execute("DELETE FROM tags WHERE id=?", (tid,))
    return {"ok": True}


# ----------------------------------------------------------------- 预览图墙（ffmpeg 抽帧）
def _ffprobe_duration(path: str, ffprobe: str) -> Optional[float]:
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
        ).stdout.decode().strip()
        return float(out) if out else None
    except Exception:
        return None


def generate_previews(media_path: str, out_dir: str, ffmpeg: str = "ffmpeg",
                      count: int = 6) -> Optional[List[str]]:
    """用 ffmpeg 在影片中等距抽 count 帧。ffmpeg 不存在 / 探测失败返回 None。"""
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe")
    dur = _ffprobe_duration(media_path, ffprobe)
    if not dur or dur <= 0:
        return None
    os.makedirs(out_dir, exist_ok=True)
    paths: List[str] = []
    for i in range(count):
        t = dur * (i + 1) / (count + 1)
        outp = os.path.join(out_dir, f"shot_{i + 1:02d}.jpg")
        cmd = [ffmpeg, "-y", "-ss", f"{t:.2f}", "-i", media_path,
               "-frames:v", "1", "-q:v", "3", outp]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30, check=True)
        except Exception:
            continue
        if os.path.exists(outp):
            paths.append(outp)
    return paths or None


def get_previews(conn: sqlite3.Connection, movie_id: int) -> List[str]:
    row = conn.execute("SELECT paths FROM movie_previews WHERE movie_id=?", (movie_id,)).fetchone()
    return _parsej(row["paths"]) if row and row["paths"] else []


def set_previews(conn: sqlite3.Connection, movie_id: int, paths: List[str]) -> None:
    conn.execute(
        "INSERT INTO movie_previews(movie_id, paths) VALUES(?, ?) "
        "ON CONFLICT(movie_id) DO UPDATE SET paths=excluded.paths, "
        "created_at=datetime('now','localtime')",
        (movie_id, _j(paths)),
    )
