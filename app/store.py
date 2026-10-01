# -*- coding: utf-8 -*-
"""数据访问层：影片的增删改查、分类归并、检索与统计。"""
from __future__ import annotations

from datetime import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import unicodedata
from pathlib import Path
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


def _smart_code(s: Any) -> str:
    """番号智能归一：全角转半角、转小写、去掉 - _ 空格 等分隔符。
    这样 'MMC-004' / 'mmc004' / 'ＭＭＣ004' 都能互相命中。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = s.lower()
    s = re.sub(r"[\s\-_/．.。·]", "", s)
    return s


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


def _find_moved_file(conn: sqlite3.Connection, movie_id: int, parsed: Dict[str, Any],
                     size: int, quick_hash: int) -> Optional[Dict[str, Any]]:
    """在「同一影片」的缺失记录里，找出本次扫描文件对应的旧记录（移动/改名识别）。

    匹配优先级：
    1. 内容指纹 + 体积：最可靠，能覆盖「改名后移动」（需要扫描时计算指纹）；
    2. 文件名相同：只换了目录、没改名，最常见；
    3. 该影片只有唯一一条缺失记录：几乎可以确定就是它被移动/改名了。
    找不到返回 None（当作真正的全新文件）。
    """
    if quick_hash:
        r = query_one(
            conn,
            "SELECT * FROM movie_files WHERE movie_id=? AND missing=1 "
            "AND quick_hash=? AND size=? LIMIT 1",
            (movie_id, quick_hash, size))
        if r:
            return r
    r = query_one(
        conn,
        "SELECT * FROM movie_files WHERE movie_id=? AND missing=1 AND filename=? LIMIT 1",
        (movie_id, parsed["filename"]))
    if r:
        return r
    rows = query_all(
        conn,
        "SELECT * FROM movie_files WHERE movie_id=? AND missing=1 LIMIT 2",
        (movie_id,))
    if len(rows) == 1:
        return rows[0]
    return None


def upsert_scanned_file(conn: sqlite3.Connection, parsed: Dict[str, Any],
                        path: str, size: int, mtime: float, quick_hash: int = 0) -> str:
    """把一个扫描到的视频文件写入库，返回 'added' / 'updated' / 'unchanged'。"""
    movie = query_one(conn, "SELECT * FROM movies WHERE key = ?", (parsed["key"],))
    if movie is None:
        cur = conn.execute(
            """INSERT INTO movies(key, code, code_norm, has_code, code_rule, title, folder,
                                  subtitle, uncensored, leak, hd4k, vr, resolution)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                parsed["key"], parsed["code"], _smart_code(parsed["code"]), parsed["has_code"], parsed["code_rule"],
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
        # 先判断是否「移动 / 改名」：同一影片下已有一条 missing 的旧记录。
        # 命中则原地更新路径，避免「新增一条 + 旧记录残留 missing」——
        # 后者会让详情 / 播放仍指向旧路径，表现为「提示文件不存在、无法播放」。
        moved = _find_moved_file(conn, movie_id, parsed, size, quick_hash)
        if moved:
            conn.execute(
                """UPDATE movie_files SET path = ?, filename = ?, ext = ?, part = ?,
                       size = ?, mtime = ?, missing = 0, missing_since = '', quick_hash = ?
                   WHERE id = ?""",
                (path, parsed["filename"], parsed["ext"], parsed["part"],
                 size, mtime, quick_hash, moved["id"]),
            )
            result = "updated"
        else:
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
            "UPDATE movie_files SET size = ?, mtime = ?, missing = 0, missing_since = '', "
            "movie_id = ?, quick_hash = ? WHERE id = ?",
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


def _days_since(ts: str) -> float:
    """返回时间戳距今天数；解析失败返回 0（视为刚发生，宁可多保留）。"""
    from datetime import datetime
    s = (ts or "").strip()
    if not s:
        return 0.0
    for fmt, cut in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return (datetime.now() - datetime.strptime(s[:cut], fmt)).total_seconds() / 86400.0
        except Exception:
            continue
    return 0.0


def prune_missing(conn: sqlite3.Connection, alive_paths: set[str], roots: Sequence[str],
                  grace_days: int = 0) -> int:
    """清理「磁盘上确已消失」的文件记录，并连带清理空影片。

    grace_days > 0 时只清理「标记缺失已超过宽限期」的记录：文件刚消失（或外置盘
    临时掉线）只保留为 missing，留给用户插回磁盘 / 搬回文件恢复的机会，避免误删。
    grace_days = 0 时保持旧行为（发现即清理）。
    """
    if not roots:
        return 0
    removed = 0
    lowered_roots = [r.replace("\\", "/").lower().rstrip("/") for r in roots]
    rows = conn.execute(
        "SELECT id, path, movie_id, missing, missing_since FROM movie_files").fetchall()
    for row in rows:
        p = row["path"].replace("\\", "/").lower()
        if not any(p.startswith(r + "/") or p == r for r in lowered_roots):
            continue
        if row["path"] in alive_paths:
            continue
        if int(row["missing"] or 0) == 0:
            # 未被标记为缺失（例如直接调用清理）：保守跳过，交由扫描先标记。
            continue
        if grace_days > 0:
            since = row["missing_since"] or ""
            # 没有首次缺失时间（旧库遗留）的，视为刚发现，等下次扫描再判定。
            if not since or _days_since(since) < grace_days:
                continue
        conn.execute("DELETE FROM movie_files WHERE id = ?", (row["id"],))
        removed += 1
    conn.execute("DELETE FROM movies WHERE id NOT IN (SELECT DISTINCT movie_id FROM movie_files)")
    return removed


def check_missing_files(conn: sqlite3.Connection) -> Dict[str, Any]:
    """快速核对：只检查库里已有路径是否还存在（不遍历目录）。

    比全盘扫描快得多，可随手点一次。发现消失的文件就标记 missing 并记录
    首次缺失时间（宽限期清理据此计算）。返回核对与新增缺失的数量。
    """
    rows = query_all(
        conn,
        "SELECT id, path FROM movie_files WHERE COALESCE(missing,0)=0",
    )
    checked = 0
    marked = 0
    for r in rows:
        checked += 1
        p = str(r["path"] or "")
        if not p or os.path.exists(p):
            continue
        conn.execute(
            "UPDATE movie_files SET missing = 1, missing_since = ? "
            "WHERE id = ? AND COALESCE(missing,0) = 0",
            (datetime_now(), r["id"]),
        )
        marked += 1
    if marked:
        conn.execute("DELETE FROM movies WHERE id NOT IN (SELECT DISTINCT movie_id FROM movie_files)")
    return {"checked": checked, "marked": marked}


def datetime_now() -> str:
    """本地时间字符串（与 SQLite datetime('now','localtime') 格式一致）。"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
       (SELECT group_concat(t.name, '||') FROM movie_tag mt
          JOIN tags t ON t.id = mt.tag_id WHERE mt.movie_id = m.id) AS tag_str,
       COALESCE(wp.position, 0) AS progress_seconds,
       COALESCE(wp.duration, 0) AS duration_seconds,
       COALESCE(wp.finished, 0) AS progress_finished,
       (SELECT COUNT(*) FROM movie_files f
          WHERE f.movie_id = m.id AND COALESCE(f.missing, 0) = 0) AS playable_cnt
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
        code_like = f"%{_smart_code(q)}%"
        clauses.append(
            "(m.code_norm LIKE ?"
            " OR m.title LIKE ? OR m.original_title LIKE ? OR m.plot LIKE ?"
            " OR m.director LIKE ? OR m.folder LIKE ?"
            " OR EXISTS(SELECT 1 FROM movie_actress ma JOIN actresses a ON a.id = ma.actress_id"
            "           WHERE ma.movie_id = m.id AND a.name LIKE ?)"
            " OR EXISTS(SELECT 1 FROM movie_files f WHERE f.movie_id = m.id AND f.filename LIKE ?))"
        )
        args.append(code_like)
        args.extend([like] * 7)

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
    row["tags"] = [x for x in (row.pop("tag_str", None) or "").split("||") if x]
    row["studio"] = row.get("studio") or ""
    row["series"] = row.get("series") or ""
    row["watchlist"] = int(row.get("watchlist") or 0)
    row["playable"] = int(row.get("playable_cnt") or 0) > 0
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
    # 只挑仍然存在的文件：缺 missing 过滤时，移动/改名后残留的旧记录会排在前面，
    # 导致流式播放 404「视频文件不存在」。
    row = query_one(conn,
        "SELECT path FROM movie_files WHERE movie_id=? AND missing=0 "
        "ORDER BY part ASC, size DESC LIMIT 1",
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

    # 播放方式分布
    by_method = [{'method': x['method'] or 'external', 'sessions': int(x['c']),
                  'sec': float(x['ws'])}
                 for x in query_all(conn,
                 "SELECT method, COUNT(*) AS c, COALESCE(SUM(watched_sec),0) AS ws "
                 "FROM watch_sessions GROUP BY method ORDER BY c DESC")]

    # 月度趋势（观看时长）
    by_month = [{'month': x['ym'], 'sessions': int(x['c']), 'sec': float(x['ws'])}
                for x in query_all(conn,
                "SELECT strftime('%Y-%m', started_at) AS ym, COUNT(*) AS c, "
                "COALESCE(SUM(watched_sec),0) AS ws FROM watch_sessions "
                "GROUP BY ym ORDER BY ym")]

    # ---- 完成度与复看 ----
    comp = query_one(conn, """
        SELECT
            COUNT(*) AS sessions,
            SUM(CASE WHEN finished=1 THEN 1 ELSE 0 END) AS finished_s,
            COALESCE(SUM(s.watched_sec),0) AS ws,
            COALESCE(SUM(CASE WHEN m.runtime>0 THEN s.watched_sec END),0) AS ws_with_rt,
            COALESCE(SUM(CASE WHEN m.runtime>0 THEN m.runtime*60.0 END),0) AS rt_total
        FROM watch_sessions s
        LEFT JOIN movies m ON m.id=s.movie_id""") or {}
    sessions_n = int(comp.get('sessions') or 0)
    finished_n = int(comp.get('finished_s') or 0)
    overall_rate = (float(comp.get('ws_with_rt') or 0) /
                    float(comp.get('rt_total') or 1)) if comp.get('rt_total') else 0.0
    completion = {
        'finish_rate': (finished_n / sessions_n) if sessions_n else 0.0,
        'avg_completion': min(1.0, overall_rate),
    }

    # 复看：每片被看次数，含二刷及以上占比
    rew = query_all(conn,
        "SELECT movie_id, COUNT(*) AS c FROM watch_sessions GROUP BY movie_id")
    movie_count = len(rew)
    rewatched = [r for r in rew if int(r['c']) >= 2]
    avg_rewatch = (sum(int(r['c']) for r in rew) / movie_count) if movie_count else 0.0
    rewatch = {
        'rewatched_movies': len(rewatched),
        'rewatch_rate': (len(rewatched) / movie_count) if movie_count else 0.0,
        'avg_sessions_per_movie': avg_rewatch,
        'top': [{'movie_id': r['movie_id'], 'sessions': int(r['c'])}
                for r in sorted(rew, key=lambda x: -int(x['c']))[:10]],
    }

    # ---- 星期节律 ----
    by_weekday = []
    for r in query_all(conn,
        "SELECT CAST(strftime('%w', started_at) AS INTEGER) AS wd, "
        "COUNT(*) AS c, COALESCE(SUM(watched_sec),0) AS ws "
        "FROM watch_sessions GROUP BY wd ORDER BY wd"):
        by_weekday.append({'wd': int(r['wd']), 'sessions': int(r['c']),
                           'sec': float(r['ws'])})
    # 工作日为 1-5，周末 0,6
    work_s = sum(x['sec'] for x in by_weekday if x['wd'] in (1, 2, 3, 4, 5))
    week_s = sum(x['sec'] for x in by_weekday if x['wd'] in (0, 6))
    weekday_split = {'workday_sec': work_s, 'weekend_sec': week_s,
                     'ratio': (work_s / week_s) if week_s else (1.0 if work_s else 0.0)}

    # ---- 连续打卡 / 空窗 ----
    days = sorted({r['d'] for r in query_all(conn,
        "SELECT DATE(started_at) AS d FROM watch_sessions")})
    streak_cur = streak_max = 0
    gap_max = 0
    if days:
        streak_cur = streak_max = 1
        max_gap = None
        for i in range(1, len(days)):
            d0 = datetime.strptime(days[i - 1], '%Y-%m-%d')
            d1 = datetime.strptime(days[i], '%Y-%m-%d')
            delta = (d1 - d0).days
            if delta == 1:
                streak_cur += 1
                streak_max = max(streak_max, streak_cur)
            else:
                streak_cur = 1
                if max_gap is None or delta > max_gap:
                    max_gap = delta
        gap_max = max_gap or 0
    # 当前连续（从最近一天往前数）
    streak_now = 0
    if days:
        streak_now = 1
        for i in range(len(days) - 1, 0, -1):
            d0 = datetime.strptime(days[i - 1], '%Y-%m-%d')
            d1 = datetime.strptime(days[i], '%Y-%m-%d')
            if (d1 - d0).days == 1:
                streak_now += 1
            else:
                break
    streak = {'current': streak_now, 'max': streak_max, 'longest_gap_days': gap_max}

    # ---- 近 12 周 vs 前 12 周 活跃度 ----
    wk = query_all(conn,
        "SELECT strftime('%Y-W%W', started_at) AS yw, "
        "COUNT(*) AS c, COALESCE(SUM(watched_sec),0) AS ws "
        "FROM watch_sessions GROUP BY yw ORDER BY yw")
    if len(wk) >= 2:
        half = len(wk) // 2
        recent_ws = sum(float(x['ws']) for x in wk[half:])
        older_ws = sum(float(x['ws']) for x in wk[:half]) or 1
        trend = {'recent_sec': recent_ws,
                 'older_sec': sum(float(x['ws']) for x in wk[:half]),
                 'growth': (recent_ws / older_ws) - 1.0}
    else:
        trend = {'recent_sec': 0.0, 'older_sec': 0.0, 'growth': 0.0}

    # ---- 偏好 × 内容 结合分析（需 join movies 属性）----
    def join_agg(sql):
        return query_all(conn, sql)

    # 评分档 × 平均观看时长/次数
    rating_x = []
    for r in join_agg(
        "SELECT CASE WHEN m.rating>=4 THEN '4-5' WHEN m.rating>=3 THEN '3-4' "
        "WHEN m.rating>0 THEN '1-3' ELSE 'unrated' END AS band, "
        "COUNT(*) AS c, COALESCE(SUM(s.watched_sec),0) AS ws "
        "FROM watch_sessions s JOIN movies m ON m.id=s.movie_id GROUP BY band"):
        rating_x.append({'band': r['band'], 'sessions': int(r['c']),
                         'sec': float(r['ws']),
                         'avg_sec': (float(r['ws']) / int(r['c'])) if r['c'] else 0.0})
    # 无码 vs 有码
    unc_x = []
    for r in join_agg(
        "SELECT CASE WHEN m.uncensored=1 THEN 'uncensored' ELSE 'censored' END AS k, "
        "COUNT(*) AS c, COALESCE(SUM(s.watched_sec),0) AS ws, "
        "COUNT(DISTINCT s.movie_id) AS mv FROM watch_sessions s "
        "JOIN movies m ON m.id=s.movie_id GROUP BY k"):
        unc_x.append({'key': r['k'], 'sessions': int(r['c']),
                      'sec': float(r['ws']), 'movies': int(r['mv'])})
    # 时长档位：观看分布 vs 库存量分布
    def bucket(minutes):
        if minutes <= 0:
            return 'unknown'
        if minutes < 40:
            return 'lt40'
        if minutes <= 90:
            return '40_90'
        return 'gt90'
    watched_buckets = {'lt40': 0.0, '40_90': 0.0, 'gt90': 0.0, 'unknown': 0.0}
    for r in join_agg(
        "SELECT m.runtime AS rt, COALESCE(SUM(s.watched_sec),0) AS ws "
        "FROM watch_sessions s JOIN movies m ON m.id=s.movie_id GROUP BY s.movie_id"):
        watched_buckets[bucket(int(r['rt'] or 0))] += float(r['ws'] or 0)
    library_buckets = {'lt40': 0, '40_90': 0, 'gt90': 0, 'unknown': 0}
    for r in query_all(conn, "SELECT runtime FROM movies WHERE runtime>0"):
        library_buckets[bucket(int(r['runtime'] or 0))] += 1
    duration_profile = {'watched': watched_buckets, 'library': library_buckets}

    return {
        'total_sec': total_sec,
        'sessions': sessions,
        'movies': int(tot.get('movies') or 0),
        'days': int(tot.get('days') or 0),
        'first_at': tot.get('first_at'),
        'last_at': tot.get('last_at'),
        'avg_session_sec': (total_sec / sessions) if sessions else 0,
        'by_hour': by_hour,
        'by_method': by_method,
        'by_month': by_month,
        'profile': {'genres': genres, 'actresses': actresses, 'studios': studios,
                    'series': series, 'directors': directors},
        'recent': [dict(r) for r in recent],
        'top_movies': [dict(r) for r in top_movies],
        # 新增：完成度 / 复看 / 时间节律 / 结合分析
        'completion': completion,
        'rewatch': rewatch,
        'by_weekday': by_weekday,
        'weekday_split': weekday_split,
        'streak': streak,
        'trend': trend,
        'rating_x': rating_x,
        'uncensored_x': unc_x,
        'duration_profile': duration_profile,
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
                    size: int = 24, sort: str = "date") -> Optional[Dict[str, Any]]:
    """女优详情：基本信息 + 其出演的影片（分页卡片）。ident 可为 id 或名称。

    sort: date=按上映时间倒序（默认）, rating=按评分倒序, favorite=按收藏,
          watched=按最近观看, recent=按最近入库。
    """
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
    info = {k: a.get(k) for k in ("id", "name", "alias", "avatar", "birthday", "note", "favorite",
                                  "height", "bust", "waist", "hip", "cup",
                                  "birthplace", "hobby", "profile")}
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
    order = {
        "date": "m.release_date DESC, m.id DESC",
        "rating": "COALESCE(m.rating, 0) DESC, m.release_date DESC, m.id DESC",
        "favorite": "COALESCE(m.favorite, 0) DESC, m.release_date DESC, m.id DESC",
        "watched": "COALESCE(m.last_played, '') DESC, m.id DESC",
        "recent": "m.id DESC",
    }.get(sort, "m.release_date DESC, m.id DESC")
    rows = query_all(
        conn,
        f"SELECT m.* FROM movie_actress ma JOIN movies m ON m.id = ma.movie_id "
        f"WHERE ma.actress_id = ? ORDER BY {order} LIMIT ? OFFSET ?",
        (aid, size, offset),
    )
    return {
        "info": info,
        "total": total,
        "page": page,
        "pages": pages,
        "items": [_row_to_card(r) for r in rows],
    }


def rename_actress(conn: sqlite3.Connection, actress_id: int, new_name: str) -> Dict[str, Any]:
    """重命名女优：改名并同步更新该女优出演的所有影片的关联关系。

    若新名称已存在同名的其他女优记录，则执行合并（避免唯一约束冲突）。
    """
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("名字不能为空")
    old = conn.execute("SELECT * FROM actresses WHERE id=?", (actress_id,)).fetchone()
    if not old:
        raise ValueError("女优不存在")
    if old["name"] == new_name:
        return {"ok": True, "merged": False}
    existing = conn.execute("SELECT id FROM actresses WHERE name=? AND id<>?", (new_name, actress_id)).fetchone()
    if existing:
        # 同名已存在 → 合并当前到 existing
        return merge_actresses(conn, actress_id, existing["id"], delete_source=True)
    conn.execute("UPDATE actresses SET name=? WHERE id=?", (new_name, actress_id))
    return {"ok": True, "merged": False}


def merge_actresses(conn: sqlite3.Connection, source_id: int, target_id: int,
                    delete_source: bool = True) -> Dict[str, Any]:
    """合并女优：把 source 的影片关联、收藏/关注/档案信息归并到 target，再删除 source。

    - 影片关联：source 的 movie_actress 转移到 target（按 movie 去重）
    - 档案：target 缺失的字段用 source 补全
    - 收藏/关注：任一为 1 则保留
    """
    if source_id == target_id:
        return {"ok": True, "merged": False}
    for aid in (source_id, target_id):
        if not conn.execute("SELECT id FROM actresses WHERE id=?", (aid,)).fetchone():
            raise ValueError("女优不存在")

    # 1) 转移影片关联（去重）
    conn.execute(
        "INSERT OR IGNORE INTO movie_actress (movie_id, actress_id) "
        "SELECT movie_id, ? FROM movie_actress WHERE actress_id=?",
        (target_id, source_id),
    )
    conn.execute("DELETE FROM movie_actress WHERE actress_id=?", (source_id,))
    # 2) 补全档案字段
    src = conn.execute("SELECT * FROM actresses WHERE id=?", (source_id,)).fetchone()
    tgt = conn.execute("SELECT * FROM actresses WHERE id=?", (target_id,)).fetchone()
    for f in ("alias", "avatar", "birthday", "note", "height", "bust", "waist", "hip", "cup",
              "birthplace", "hobby", "profile"):
        if not tgt.get(f) and src.get(f):
            conn.execute(f"UPDATE actresses SET {f}=? WHERE id=?", (src[f], target_id))
    # 3) 收藏/关注取并集
    conn.execute(
        "UPDATE actresses SET favorite = MAX(favorite, ?), followed = MAX(followed, ?) WHERE id=?",
        (src["favorite"] or 0, src["followed"] or 0, target_id),
    )
    moved = int(scalar(conn,
                       "SELECT COUNT(*) FROM movie_actress WHERE actress_id=?", (target_id,)) or 0)
    if delete_source:
        conn.execute("DELETE FROM actresses WHERE id=?", (source_id,))
    return {"ok": True, "merged": True, "target_id": target_id, "total_movies": moved}


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
        "WHERE movie_id = ? ORDER BY missing ASC, part, filename",
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
        sets.append("code_norm = ?")
        args.append(_smart_code(payload["code"]))

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


def recent_watch(conn: sqlite3.Connection, page: int = 1, size: int = 60) -> Dict[str, Any]:
    """返回「最近观看」的影片（按最近一次观看去重），含观看进度与最近观看时间。

    与 continue_watching 不同：后者只含未看完；这里包含全部看过的影片（含看完的），
    按 watch_sessions.started_at 最近一次倒序去重，供「最近观看」片单式预览与连播。
    """
    page = max(1, int(page or 1))
    size = min(200, max(1, int(size or 60)))
    offset = (page - 1) * size

    # 每个影片取最近一次观看的 started_at
    total = int(scalar(conn, "SELECT COUNT(DISTINCT movie_id) FROM watch_sessions") or 0)
    ids = [r["movie_id"] for r in query_all(
        conn,
        """SELECT movie_id FROM watch_sessions
           GROUP BY movie_id ORDER BY MAX(started_at) DESC, movie_id DESC
           LIMIT ? OFFSET ?""", (size, offset))]

    items: list = []
    if ids:
        placeholders = ",".join("?" * len(ids))
        rows = query_all(conn, f"{_LIST_SELECT} WHERE m.id IN ({placeholders})", ids)
        prog = {r["movie_id"]: r for r in query_all(
            conn,
            f"SELECT movie_id, position, duration FROM watch_progress WHERE movie_id IN ({placeholders})",
            ids)}
        last_seen = {r["movie_id"]: r["started_at"] for r in query_all(
            conn,
            f"""SELECT movie_id, MAX(started_at) AS started_at FROM watch_sessions
                WHERE movie_id IN ({placeholders}) GROUP BY movie_id""", ids)}
        order = {mid: i for i, mid in enumerate(ids)}
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
            card["last_played_at"] = last_seen.get(card["id"])
            items.append(card)
        items.sort(key=lambda c: order.get(c["id"], 999))

    return {"items": items, "total": total, "page": page, "page_size": size,
            "pages": max(1, (total + size - 1) // size)}


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

# 系统预置片单：随影片库自动更新，不可删除/手动编辑。
# 本质是按特殊排序键动态聚合的 smart 片单。
SYSTEM_COLLECTIONS = [
    {"key": "top_played", "name": "播放最多", "sort": "play_desc", "desc": "按播放次数从高到低"},
    {"key": "top_rated", "name": "评分最高", "sort": "rating_desc", "desc": "按用户评分从高到低"},
    {"key": "most_actresses", "name": "群星荟萃", "sort": "actress_count_desc", "desc": "出演女优最多的作品"},
    {"key": "latest_added", "name": "最近入库", "sort": "added_desc", "desc": "最近加入影片库的影片"},
]


def ensure_system_collections(conn: sqlite3.Connection) -> None:
    """保证系统片单存在（按 system_key 去重），已存在则跳过。"""
    existing = {r["system_key"] for r in query_all(conn, "SELECT system_key FROM collections WHERE system_key <> ''")}
    for sc in SYSTEM_COLLECTIONS:
        if sc["key"] in existing:
            continue
        create_collection(
            conn, sc["name"], kind="system",
            rule={"params": {"sort": sc["sort"]}},
            system_key=sc["key"],
        )


def system_collection_desc(system_key: str) -> str:
    for sc in SYSTEM_COLLECTIONS:
        if sc["key"] == system_key:
            return sc["desc"]
    return ""


def list_collections(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = query_all(
        conn,
        """SELECT c.id, c.name, c.kind, c.rule, c.system_key, c.cover_movie_id,
                  (SELECT COUNT(*) FROM collection_items ci WHERE ci.collection_id = c.id) AS count,
                  (SELECT m.cover FROM collection_items ci JOIN movies m ON m.id = ci.movie_id
                      WHERE ci.collection_id = c.id AND m.cover <> '' LIMIT 1) AS cover,
                  (SELECT m.id FROM collection_items ci JOIN movies m ON m.id = ci.movie_id
                      WHERE ci.collection_id = c.id AND m.cover <> '' LIMIT 1) AS cover_id
           FROM collections c ORDER BY c.created_at DESC, c.id DESC""",
    )
    out = []
    for r in rows:
        d = dict(r)
        if (d.get("kind") or "manual") != "manual":
            # 智能片单：影片由规则动态计算，不存 collection_items，需实时统计
            try:
                params = _smart_params(d.get("rule"))
            except Exception:
                params = {}
            res = search_movies(conn, {**params, "page_size": 3})
            d["count"] = int(res.get("total") or 0)
            previews = [m for m in res.get("items", []) if m.get("cover")]
            d["preview_ids"] = [int(m["id"]) for m in previews[:3]]
            if previews:
                d["cover"] = previews[0]["cover"]
                d["cover_id"] = int(previews[0]["id"])
            else:
                d["cover"] = ""
                d["cover_id"] = None
            d["rule_params"] = params
            if d.get("kind") == "system":
                d["system_desc"] = system_collection_desc(d.get("system_key") or "")
        else:
            # 前 3 部有封面的影片 id，用于列表拼图预览
            previews = query_all(
                conn,
                """SELECT m.id FROM collection_items ci
                     JOIN movies m ON m.id = ci.movie_id
                     WHERE ci.collection_id = ? AND m.cover <> ''
                     ORDER BY ci.position ASC LIMIT 3""",
                (d["id"],),
            )
            d["preview_ids"] = [int(x["id"]) for x in previews]
        out.append(d)
    return out


def _smart_params(rule: Any) -> Dict[str, Any]:
    """从 collections.rule 解析出 search_movies 用的参数。"""
    if isinstance(rule, str):
        if not rule:
            return {}
        try:
            rule = json.loads(rule)
        except Exception:
            return {}
    if isinstance(rule, dict):
        return rule.get("params") or {}
    return {}


def create_collection(conn: sqlite3.Connection, name: str, kind: str = "manual", rule: Any = "",
                      system_key: str = "") -> int:
    name = norm_name(name)
    if not name:
        raise ValueError("片单名称不能为空")
    if isinstance(rule, (dict, list)):
        rule = _j(rule)
    return int(conn.execute(
        "INSERT INTO collections(name, kind, rule, system_key) VALUES(?,?,?,?)",
        (name, kind, rule or "", system_key or "")).lastrowid)


def rename_collection(conn: sqlite3.Connection, cid: int, name: str) -> None:
    name = norm_name(name)
    if not name:
        raise ValueError("片单名称不能为空")
    conn.execute("UPDATE collections SET name = ? WHERE id = ?", (name, cid))


def delete_collection(conn: sqlite3.Connection, cid: int) -> None:
    info = conn.execute("SELECT kind FROM collections WHERE id=?", (cid,)).fetchone()
    if info and (info["kind"] or "manual") == "system":
        raise ValueError("系统片单不可删除")
    conn.execute("DELETE FROM collections WHERE id = ?", (cid,))


def add_to_collection(conn: sqlite3.Connection, cid: int, movie_id: int) -> None:
    info = conn.execute("SELECT kind FROM collections WHERE id=?", (cid,)).fetchone()
    if info and (info["kind"] or "manual") != "manual":
        raise ValueError("智能/系统清单由规则自动生成，不能手动添加")
    conn.execute(
        "INSERT OR IGNORE INTO collection_items(collection_id, movie_id, position) "
        "SELECT ?, ?, COALESCE(MAX(position), 0) + 1 FROM collection_items WHERE collection_id = ?",
        (cid, movie_id, cid))


def remove_from_collection(conn: sqlite3.Connection, cid: int, movie_id: int) -> None:
    conn.execute(
        "DELETE FROM collection_items WHERE collection_id = ? AND movie_id = ?", (cid, movie_id))


def reorder_collection(conn: sqlite3.Connection, cid: int, ordered_ids: List[int]) -> None:
    """按给定的 movie_id 顺序重排手动清单（仅 manual 生效）。"""
    info = conn.execute("SELECT kind FROM collections WHERE id=?", (cid,)).fetchone()
    if info and (info["kind"] or "manual") == "smart":
        raise ValueError("智能清单由规则自动生成，不能手动排序")
    # 仅保留确实属于该清单的 id，按传入顺序写回 position
    valid = {r[0] for r in conn.execute(
        "SELECT movie_id FROM collection_items WHERE collection_id = ?", (cid,)).fetchall()}
    seq = [mid for mid in ordered_ids if mid in valid]
    for pos, mid in enumerate(seq, start=1):
        conn.execute(
            "UPDATE collection_items SET position = ? WHERE collection_id = ? AND movie_id = ?",
            (pos, cid, mid))


def get_collection_playhead(conn: sqlite3.Connection, cid: int):
    row = conn.execute(
        "SELECT movie_id FROM collection_playhead WHERE collection_id = ?", (cid,)).fetchone()
    return row["movie_id"] if row else None


def set_collection_playhead(conn: sqlite3.Connection, cid: int, movie_id: int) -> None:
    conn.execute(
        "INSERT INTO collection_playhead(collection_id, movie_id) VALUES(?, ?) "
        "ON CONFLICT(collection_id) DO UPDATE SET movie_id = excluded.movie_id, "
        "updated_at = datetime('now','localtime')",
        (cid, movie_id))


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
    if info and (info["kind"] or "manual") != "manual":
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
        "playhead": get_collection_playhead(conn, cid),
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


def _dir_size(p: Path) -> Tuple[int, int]:
    """统计目录下文件个数与总字节数（不递归子目录的文件数按需）。"""
    if not p.exists() or not p.is_dir():
        return 0, 0
    n = 0
    total = 0
    for f in p.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
                n += 1
            except OSError:
                pass
    return n, total


def _list_temp_files() -> List[Path]:
    """covers 下的临时抽帧/候选文件（_extract_ / _cand_ / _dbg_）。"""
    from .config import COVER_DIR as _CD
    if not _CD.is_dir():
        return []
    return [f for f in _CD.iterdir()
            if f.is_file() and f.name.startswith(("_extract_", "_cand_", "_dbg_"))]


def software_storage_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    """统计软件自身的数据目录占用（封面、预览图、头像、背景图、临时文件、数据库等）。

    并识别出可清理项（临时文件、孤儿预览图），供维护页展示。"""
    from .config import AVATAR_DIR, FANART_DIR, DB_PATH, CONFIG_PATH, COVER_DIR
    from .config import avatar_dir, fanart_dir
    cover_n, cover_bytes = _dir_size(COVER_DIR)
    av_n, av_bytes = _dir_size(avatar_dir())
    fa_n, fa_bytes = _dir_size(fanart_dir())

    # 预览图子目录（covers/preview/）
    preview_dir = COVER_DIR / "preview"
    pv_n, pv_bytes = _dir_size(preview_dir)

    temp_files = _list_temp_files()
    temp_n = len(temp_files)
    temp_bytes = 0
    for f in temp_files:
        try:
            temp_bytes += f.stat().st_size
        except OSError:
            pass

    # 孤儿预览图：covers/preview/ 下的子目录 id 不在 movies 表
    valid_ids = {r["id"] for r in query_all(conn, "SELECT id FROM movies")}
    orphan_pv = 0
    orphan_pv_bytes = 0
    orphan_pv_dirs: List[str] = []
    if preview_dir.is_dir():
        for d in preview_dir.iterdir():
            if not d.is_dir():
                continue
            try:
                mid = int(d.name)
            except ValueError:
                mid = -1
            if mid not in valid_ids:
                n, b = _dir_size(d)
                orphan_pv += n
                orphan_pv_bytes += b
                orphan_pv_dirs.append(d.name)

    db_bytes = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    cfg_bytes = CONFIG_PATH.stat().st_size if CONFIG_PATH.exists() else 0

    def entry(label, n, b):
        return {"label": label, "files": n, "bytes": b}

    dirs = [
        entry("covers", cover_n, cover_bytes),
        entry("previews", pv_n, pv_bytes),
        entry("avatars", av_n, av_bytes),
        entry("fanarts", fa_n, fa_bytes),
        entry("temp", temp_n, temp_bytes),
        entry("db", 1, db_bytes),
        entry("config", 1, cfg_bytes),
    ]
    total_files = sum(d["files"] for d in dirs)
    total_bytes = sum(d["bytes"] for d in dirs)
    return {
        "dirs": dirs,
        "total": {"files": total_files, "bytes": total_bytes},
        "cleanable": {
            "temp": {"files": temp_n, "bytes": temp_bytes},
            "preview_orphan": {"files": orphan_pv, "bytes": orphan_pv_bytes,
                               "dirs": orphan_pv_dirs},
        },
    }


def clean_software_storage(conn: sqlite3.Connection, targets: List[str]) -> Dict[str, Any]:
    """按 targets 清理软件数据。targets 可取：
    - 'temp'            : 临时抽帧/候选文件
    - 'preview_orphan'  : 孤儿预览图（影片已不存在的预览目录）
    - 'preview_all'     : 全部预览图（需在详情页重新生成）
    - 'db_vacuum'       : 收缩数据库未用空间
    返回清理统计。"""
    from .config import COVER_DIR
    removed = 0
    freed = 0
    cleared: List[str] = []

    if "temp" in targets:
        for f in _list_temp_files():
            try:
                sz = f.stat().st_size
                f.unlink()
                removed += 1
                freed += sz
            except OSError:
                pass
        cleared.append("temp")

    if "preview_orphan" in targets:
        valid_ids = {r["id"] for r in query_all(conn, "SELECT id FROM movies")}
        preview_dir = COVER_DIR / "preview"
        if preview_dir.is_dir():
            for d in preview_dir.iterdir():
                if not d.is_dir():
                    continue
                try:
                    mid = int(d.name)
                except ValueError:
                    mid = -1
                if mid in valid_ids:
                    continue
                n, b = _dir_size(d)
                try:
                    import shutil as _sh
                    _sh.rmtree(d)
                    removed += n
                    freed += b
                except OSError:
                    pass
        cleared.append("preview_orphan")

    if "preview_all" in targets:
        preview_dir = COVER_DIR / "preview"
        if preview_dir.is_dir():
            for d in preview_dir.iterdir():
                if d.is_dir():
                    n, b = _dir_size(d)
                    try:
                        import shutil as _sh
                        _sh.rmtree(d)
                        removed += n
                        freed += b
                    except OSError:
                        pass
        # 清空预览记录，详情页可重新生成
        conn.execute("DELETE FROM movie_previews")
        cleared.append("preview_all")

    if "db_vacuum" in targets:
        try:
            conn.commit()
            conn.execute("VACUUM")
            cleared.append("db_vacuum")
        except Exception:
            pass

    conn.commit()
    return {"cleared": cleared, "removed": removed, "freed": freed}


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
            # 与详情页单个操作一致：在影片已有标签基础上并集追加，而非整体替换
            cur = {r[0] for r in conn.execute(
                "SELECT t.name FROM movie_tag mt JOIN tags t ON t.id = mt.tag_id WHERE mt.movie_id = ?",
                (mid,)).fetchall()}
            for t in tags:
                if t: cur.add(t)
            conn.execute("DELETE FROM movie_tag WHERE movie_id = ?", (mid,))
            for t in cur:
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


# ----------------------------------------------------------------- 智能片单下拉用高频词
def taxonomy(conn: sqlite3.Connection) -> Dict[str, Any]:
    """返回用于智能片单筛选下拉的高频类型/女优/系列，避免用户手敲。"""
    return {
        "genres": [
            {"name": r["name"], "count": int(r["count"])}
            for r in query_all(
                conn,
                "SELECT g.name, COUNT(*) AS count FROM movie_genre mg "
                "JOIN genres g ON g.id = mg.genre_id GROUP BY g.id ORDER BY count DESC LIMIT 60")
        ],
        "actresses": [
            {"name": r["name"], "count": int(r["count"])}
            for r in query_all(
                conn,
                "SELECT a.name, COUNT(*) AS count FROM movie_actress ma "
                "JOIN actresses a ON a.id = ma.actress_id GROUP BY a.id ORDER BY count DESC LIMIT 60")
        ],
        "series": [
            {"name": r["name"], "count": int(r["count"])}
            for r in query_all(
                conn,
                "SELECT se.name, COUNT(*) AS count FROM movies m "
                "JOIN series se ON se.id = m.series_id GROUP BY se.id ORDER BY count DESC LIMIT 60")
        ],
    }


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


def watch_history(conn: sqlite3.Connection, page: int = 1, size: int = 50,
                  from_: str = None, to_: str = None,
                  method: str = None, q: str = None) -> Dict[str, Any]:
    """按时间倒序返回每次观看的明细（一条观看记录一行），支持筛选与汇总。

    from_/to_: 起始/结束时间（YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS）
    method:    播放方式过滤（builtin/external/system）
    q:         番号或标题关键字
    """
    page = max(1, int(page or 1))
    size = min(200, max(1, int(size or 50)))
    offset = (page - 1) * size

    where = []
    args: list = []
    if from_:
        where.append("s.started_at >= ?")
        args.append(from_)
    if to_:
        # 兼容只给日期的情况：补齐到当天末尾
        to_val = to_
        if len(to_val) == 10:
            to_val += " 23:59:59"
        where.append("s.started_at <= ?")
        args.append(to_val)
    if method:
        where.append("s.method = ?")
        args.append(method)
    if q:
        code_like = f"%{_smart_code(q)}%"
        like = f"%{q}%"
        where.append("(m.code_norm LIKE ? OR m.title LIKE ?)")
        args.extend([code_like, like])

    wsql = (" WHERE " + " AND ".join(where)) if where else ""

    total = int(scalar(conn, f"SELECT COUNT(*) FROM watch_sessions s JOIN movies m ON m.id=s.movie_id{wsql}", args) or 0)
    rows = query_all(conn, f"""
        SELECT s.id, s.movie_id, s.started_at, s.ended_at, s.watched_sec,
               s.finished, s.method, s.start_pos, s.end_pos,
               m.code, m.title, m.cover, m.year, st.name AS studio,
               (SELECT f.filename FROM movie_files f
                  WHERE f.movie_id = m.id ORDER BY f.part ASC, f.id ASC LIMIT 1) AS file_name,
               (SELECT COUNT(*) FROM movie_files f
                  WHERE f.movie_id = m.id AND COALESCE(f.missing,0)=0) AS playable_cnt
        FROM watch_sessions s
        JOIN movies m ON m.id = s.movie_id
        LEFT JOIN studios st ON st.id = m.studio_id
        {wsql}
        ORDER BY s.started_at DESC, s.id DESC LIMIT ? OFFSET ?""", args + [size, offset])

    # 汇总（基于筛选后的全集，而非当前页）
    summ = query_one(conn, f"""
        SELECT COALESCE(SUM(s.watched_sec),0) AS total_sec,
               COUNT(DISTINCT s.movie_id) AS movies,
               COUNT(DISTINCT DATE(s.started_at)) AS days
        FROM watch_sessions s
        JOIN movies m ON m.id = s.movie_id
        {wsql}""", args) or {}

    items = [{
        "id": r["id"],
        "movie_id": r["movie_id"],
        "code": r["code"],
        "title": r["title"],
        "cover": r["cover"],
        "studio": r["studio"],
        "year": r["year"],
        "file_name": r["file_name"],
        "playable": int(r["playable_cnt"] or 0) > 0,
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
        "summary": {
            "total_sec": float(summ.get("total_sec") or 0),
            "movies": int(summ.get("movies") or 0),
            "days": int(summ.get("days") or 0),
            "sessions": total,
        },
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
    # 跨影片的"内容完全相同"文件（体检里叫重复影片）。
    # 判定依据是内容指纹 quick_hash 相同，而不是 size 相等——不同文件体积恰好相同
    # 是常态（尤其同一下载站拆出的等大分卷，如 FC2 的 xxx_1 / xxx_2），
    # 仅凭 size 会把它们误报成重复。quick_hash = size + 首尾采样哈希，几乎不会碰撞。
    #
    # 只比 f1.movie_id < f2.movie_id 的跨影片配对：同一影片里的多个文件是
    # 分卷/多版本，即使碰巧同体积也不算"跨片重复"。从未算过指纹的（quick_hash=0）
    # 不判定，避免误报，交由去重面板的精确扫描兜底。
    dup_rows = query_all(
        conn,
        "SELECT movie_id, size, quick_hash, path "
        "FROM movie_files WHERE missing=0 AND quick_hash>0")
    by_hash: Dict[tuple, List[tuple]] = {}
    for r in dup_rows:
        by_hash.setdefault((r["quick_hash"], r["size"]), []).append(
            (r["movie_id"], r["path"]))
    duplicates = []
    for (qh, sz), items in by_hash.items():
        if len(items) < 2:
            continue
        seen_movies = set()
        uniq = []
        for mid, p in items:
            if mid in seen_movies:
                continue  # 同一影片内部（分卷/多版本）不去重
            seen_movies.add(mid)
            uniq.append((mid, p))
        # 取两两里最代表性的几对：按 movie_id 升序取首对，足够提醒用户
        if len(uniq) >= 2:
            a, b = uniq[0], uniq[1]
            duplicates.append({
                "a": a[0], "b": b[0], "size": sz,
                "p1": a[1], "p2": b[1],
            })
        if len(duplicates) >= 200:
            break
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
                # extract_code 返回 (番号, 原始匹配串, 规则名)，这里要的是规则名
                new_code, used_rule = code, parsed[2] if len(parsed) > 2 else "reparse"
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


def taste_profile(conn: sqlite3.Connection, year: int = 0) -> Dict[str, Any]:
    """观影口味画像：8 个维度（0~100），全部基于真实观看行为（watch_sessions）。

    year=0 表示全部时间；否则只统计该年份开始观看的场次。
    每个维度返回 {key, value(0-100), raw(0-1), top(top 项名称, 可选)}，
    文案由前端 i18n 按 key 渲染，后端不返回中文。
    """
    yc = "substr(s.started_at,1,4)=?" if year else "1=1"
    ya = [str(year)] if year else []

    tot = query_one(
        conn,
        "SELECT COUNT(*) AS sessions, COALESCE(SUM(s.watched_sec),0) AS total_sec, "
        f"COUNT(DISTINCT s.movie_id) AS movies FROM watch_sessions s WHERE {yc}",
        tuple(ya),
    ) or {}
    sessions = int(tot.get("sessions") or 0)
    total_sec = float(tot.get("total_sec") or 0)
    movies = int(tot.get("movies") or 0)
    if sessions <= 0 or total_sec <= 0:
        return {"year": year, "dims": [],
                "totals": {"sessions": 0, "total_sec": 0, "movies": 0}}

    def pct(numer: float) -> float:
        """按时长占比换算成 0~100，用于雷达图数值。"""
        return round(max(0.0, min(100.0, (numer / total_sec) * 100.0)), 1)

    def sum_sec(sql: str, extra: tuple = ()) -> float:
        """求和观看秒数；约定年份条件 yc 放在 WHERE 末尾，因此 ya 参数放最后。"""
        row = query_one(conn, sql, tuple(extra) + tuple(ya)) or {}
        return float(row.get("ws") or 0)

    # 1 类型专注：观看时长 top3 类型占比
    g_rows = query_all(
        conn,
        "SELECT g.name AS name, SUM(s.watched_sec) AS ws FROM watch_sessions s "
        "JOIN movie_genre mg ON mg.movie_id=s.movie_id JOIN genres g ON g.id=mg.genre_id "
        f"WHERE {yc} GROUP BY g.id ORDER BY ws DESC LIMIT 3",
        tuple(ya),
    )
    genre_focus = pct(sum(float(r["ws"] or 0) for r in g_rows))

    # 2 女优专一：观看时长 top3 女优占比
    a_rows = query_all(
        conn,
        "SELECT a.name AS name, SUM(s.watched_sec) AS ws FROM watch_sessions s "
        "JOIN movie_actress ma ON ma.movie_id=s.movie_id JOIN actresses a ON a.id=ma.actress_id "
        f"WHERE {yc} GROUP BY a.id ORDER BY ws DESC LIMIT 3",
        tuple(ya),
    )
    actress_loyalty = pct(sum(float(r["ws"] or 0) for r in a_rows))

    # 3 长片偏好：单片时长 > 120 分钟的观看占比
    long_sec = sum_sec(
        "SELECT COALESCE(SUM(s.watched_sec),0) AS ws FROM watch_sessions s "
        f"JOIN movies m ON m.id=s.movie_id WHERE COALESCE(m.runtime,0)>120 AND {yc}")

    # 4 追新：发行年份 >= 今年-2 的观看占比
    import datetime as _dt
    cut = _dt.date.today().year - 2
    fresh_sec = sum_sec(
        "SELECT COALESCE(SUM(s.watched_sec),0) AS ws FROM watch_sessions s "
        f"JOIN movies m ON m.id=s.movie_id WHERE COALESCE(m.year,0)>=? AND {yc}", (cut,))

    # 5 无码倾向：无码影片的观看占比
    unc_sec = sum_sec(
        "SELECT COALESCE(SUM(s.watched_sec),0) AS ws FROM watch_sessions s "
        f"JOIN movies m ON m.id=s.movie_id WHERE COALESCE(m.uncensored,0)=1 AND {yc}")

    # 6 夜猫子：20:00~04:00 开始的观看占比
    night_sec = sum_sec(
        "SELECT COALESCE(SUM(s.watched_sec),0) AS ws FROM watch_sessions s "
        "WHERE (CAST(strftime('%H',s.started_at) AS INTEGER)>=20 OR "
        f"CAST(strftime('%H',s.started_at) AS INTEGER)<4) AND {yc}")

    # 7 看完率：标记为看完的场次占比（按场次，不按时长）
    fin = query_one(
        conn,
        f"SELECT COUNT(*) AS c FROM watch_sessions s "
        f"WHERE COALESCE(s.finished,0)=1 AND {yc}", tuple(ya)) or {}
    completion = round((int(fin.get("c") or 0) / sessions) * 100, 1) if sessions else 0.0

    # 8 重温：看过 2 次以上的影片占已看影片的比例
    rw = query_one(
        conn,
        "SELECT COUNT(*) AS c FROM (SELECT s.movie_id FROM watch_sessions s "
        f"WHERE {yc} GROUP BY s.movie_id HAVING COUNT(*)>1)", tuple(ya)) or {}
    rewatch = round((int(rw.get("c") or 0) / movies) * 100, 1) if movies else 0.0

    def dim(key: str, value: float, raw: float, top: list = None) -> Dict[str, Any]:
        return {"key": key, "value": value, "raw": round(raw, 3), "top": top or []}

    return {
        "year": year,
        "totals": {"sessions": sessions, "total_sec": total_sec, "movies": movies},
        "dims": [
            dim("genre_focus", genre_focus, genre_focus / 100.0,
                [str(r["name"]) for r in g_rows]),
            dim("actress_loyalty", actress_loyalty, actress_loyalty / 100.0,
                [str(r["name"]) for r in a_rows]),
            dim("long_form", pct(long_sec), long_sec / total_sec),
            dim("freshness", pct(fresh_sec), fresh_sec / total_sec),
            dim("uncensored", pct(unc_sec), unc_sec / total_sec),
            dim("night_owl", pct(night_sec), night_sec / total_sec),
            dim("completion", completion, completion / 100.0),
            dim("rewatch", rewatch, rewatch / 100.0),
        ],
    }


def recommend_movies(conn: sqlite3.Connection, limit: int = 30, seed: int = None,
                     exclude_watched: bool = True, explore: float = 1.0) -> Dict[str, Any]:
    """口味加权推荐：用观看历史算出偏好，对候选影片打分后**加权随机**抽取。

    偏好来源：观看时长在 类型/女优/厂商/系列 上的分布，以及时长/年份/无码倾向。
    打分后按 (score+ε)^explore 加权随机（Efraimidis-Spirakis），
    保证高分片更容易出现，但每次结果不同且冷门片也有机会，避免信息茧房。

    返回 {items:[{影片字段..., score, reasons:[{kind,name}]}], has_history, total}
    """
    import random as _rnd

    # ---- 1. 观看历史 → 偏好权重 ----
    sess = query_all(conn, "SELECT movie_id, watched_sec FROM watch_sessions")
    total_sec = sum(float(r["watched_sec"] or 0) for r in sess)
    watched_ids = {int(r["movie_id"]) for r in sess if r["movie_id"]}
    has_history = total_sec > 0 and bool(watched_ids)

    def _weights(sql: str) -> Dict[int, float]:
        """按观看时长占比归一化成 0~1 的权重。"""
        out: Dict[int, float] = {}
        if not has_history:
            return out
        for r in query_all(conn, sql):
            k = r["k"]
            v = float(r["ws"] or 0)
            if k is not None and v > 0:
                out[int(k)] = v / total_sec
        return out

    w_genre = _weights(
        "SELECT mg.genre_id AS k, SUM(s.watched_sec) AS ws FROM watch_sessions s "
        "JOIN movie_genre mg ON mg.movie_id=s.movie_id GROUP BY mg.genre_id")
    w_actress = _weights(
        "SELECT ma.actress_id AS k, SUM(s.watched_sec) AS ws FROM watch_sessions s "
        "JOIN movie_actress ma ON ma.movie_id=s.movie_id GROUP BY ma.actress_id")
    w_studio = _weights(
        "SELECT m.studio_id AS k, SUM(s.watched_sec) AS ws FROM watch_sessions s "
        "JOIN movies m ON m.id=s.movie_id WHERE m.studio_id IS NOT NULL GROUP BY m.studio_id")
    w_series = _weights(
        "SELECT m.series_id AS k, SUM(s.watched_sec) AS ws FROM watch_sessions s "
        "JOIN movies m ON m.id=s.movie_id WHERE m.series_id IS NOT NULL GROUP BY m.series_id")

    pref_dur = pref_year = pref_unc = 0.0
    if has_history:
        d_rows = query_all(
            conn,
            "SELECT COALESCE(m.runtime,0) AS rt, COALESCE(m.year,0) AS yr, "
            "COALESCE(m.uncensored,0) AS unc, s.watched_sec AS ws "
            "FROM watch_sessions s JOIN movies m ON m.id=s.movie_id")
        s_all = sum(float(r["ws"] or 0) for r in d_rows) or 1.0
        pref_dur = sum(float(r["rt"] or 0) * float(r["ws"] or 0) for r in d_rows) / s_all
        pref_year = sum(float(r["yr"] or 0) * float(r["ws"] or 0) for r in d_rows) / s_all
        pref_unc = sum(float(r["unc"] or 0) * float(r["ws"] or 0) for r in d_rows) / s_all

    # ---- 2. 候选影片 + 特征索引 ----
    movies = query_all(
        conn,
        "SELECT id, code, title, cover, year, runtime, rating, uncensored, "
        "studio_id, series_id, favorite, watchlist FROM movies")
    gmap: Dict[int, List[int]] = {}
    for r in query_all(conn, "SELECT movie_id, genre_id FROM movie_genre"):
        gmap.setdefault(int(r["movie_id"]), []).append(int(r["genre_id"]))
    amap: Dict[int, List[int]] = {}
    for r in query_all(conn, "SELECT movie_id, actress_id FROM movie_actress"):
        amap.setdefault(int(r["movie_id"]), []).append(int(r["actress_id"]))

    gname = {int(r["id"]): r["name"] for r in query_all(conn, "SELECT id, name FROM genres")}
    aname = {int(r["id"]): r["name"] for r in query_all(conn, "SELECT id, name FROM actresses")}
    sname = {int(r["id"]): r["name"] for r in query_all(conn, "SELECT id, name FROM studios")}
    sename = {int(r["id"]): r["name"] for r in query_all(conn, "SELECT id, name FROM series")}

    # ---- 3. 逐部打分 ----
    rng = _rnd.Random(seed)
    scored: List[Dict[str, Any]] = []
    for mv in movies:
        mid = int(mv["id"])
        if exclude_watched and mid in watched_ids:
            continue
        score = 0.0
        reasons: List[Dict[str, Any]] = []
        if has_history:
            # 类型（权重 3.0）
            gs = [(g, w_genre.get(g, 0.0)) for g in gmap.get(mid, [])]
            gs = [x for x in gs if x[1] > 0]
            if gs:
                top = max(gs, key=lambda x: x[1])
                score += 3.0 * sum(w for _, w in gs)
                reasons.append({"kind": "genre", "name": gname.get(top[0], ""), "w": round(top[1], 3)})
            # 女优（权重 3.5，口味里最显著的信号）
            acts = [(a, w_actress.get(a, 0.0)) for a in amap.get(mid, [])]
            acts = [x for x in acts if x[1] > 0]
            if acts:
                top = max(acts, key=lambda x: x[1])
                score += 3.5 * sum(w for _, w in acts)
                reasons.append({"kind": "actress", "name": aname.get(top[0], ""), "w": round(top[1], 3)})
            # 厂商 / 系列
            if mv["studio_id"] and w_studio.get(int(mv["studio_id"])):
                score += 1.0 * w_studio[int(mv["studio_id"])]
                reasons.append({"kind": "studio", "name": sname.get(int(mv["studio_id"]), ""),
                                "w": round(w_studio[int(mv["studio_id"])], 3)})
            if mv["series_id"] and w_series.get(int(mv["series_id"])):
                score += 0.8 * w_series[int(mv["series_id"])]
                reasons.append({"kind": "series", "name": sename.get(int(mv["series_id"]), ""),
                                "w": round(w_series[int(mv["series_id"])], 3)})
            # 时长契合（0~1.2）
            rt = float(mv["runtime"] or 0)
            if pref_dur > 0 and rt > 0:
                fit = 1.0 - min(1.0, abs(rt - pref_dur) / max(60.0, pref_dur))
                if fit > 0:
                    score += 1.2 * fit
                    if fit > 0.6:
                        reasons.append({"kind": "runtime", "name": "", "w": round(fit, 3)})
            # 年份契合（0~0.6）
            yr = float(mv["year"] or 0)
            if pref_year > 0 and yr > 0:
                fit = 1.0 - min(1.0, abs(yr - pref_year) / 12.0)
                if fit > 0:
                    score += 0.6 * fit
                    if fit > 0.75:
                        reasons.append({"kind": "year", "name": "", "w": round(fit, 3)})
            # 无码倾向契合
            unc = 1.0 if (mv["uncensored"] or 0) else 0.0
            score += 0.5 * (1.0 - abs(unc - pref_unc))
        else:
            # 无观看历史：按评分 / 收藏兜底（冷启动）
            score = float(mv["rating"] or 0) / 2.0 + (0.5 if mv["favorite"] else 0.0)
        # 轻微随机扰动：每次结果不同，且给冷门片出头机会
        score *= 0.85 + 0.3 * rng.random()
        reasons.sort(key=lambda x: -x["w"])
        scored.append({
            "id": mid, "code": mv["code"], "title": mv["title"], "cover": mv["cover"],
            "year": mv["year"], "runtime": mv["runtime"], "rating": mv["rating"],
            "uncensored": mv["uncensored"], "favorite": mv["favorite"],
            "watchlist": mv["watchlist"],
            "score": round(score, 4), "reasons": reasons[:3],
        })

    # ---- 4. 加权随机排序（避免信息茧房） ----
    exp = max(0.0, float(explore or 1.0))
    for it in scored:
        w = max(1e-6, (it["score"] + 0.15)) ** exp
        it["_k"] = rng.random() ** (1.0 / w)
    scored.sort(key=lambda x: -x["_k"])
    items = scored[:max(1, int(limit or 30))]
    for it in items:
        it.pop("_k", None)
    return {"items": items, "has_history": has_history,
            "total": len(scored), "explore": exp}


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
                      count: int = 0, quality: str = "smart") -> Optional[List[str]]:
    """用 ffmpeg 在影片中等距抽 count 帧。ffmpeg 不存在 / 探测失败返回 None。

    抽帧为等距（t = dur*(i+1)/(count+1)），前端可按比例反推帧下标：
    index = round(ratio*(count+1)) - 1。

    count 为 0 时按 quality 档位 + 时长自适应：
      smart：每 ~5 分钟一帧，8~24 张（均衡，默认）
      low  ：每 ~8 分钟一帧，6~12 张（省空间、生成快）
      high ：每 ~3 分钟一帧，12~40 张（精细预览）

    注：ffmpeg 可能是 PATH 名（"ffmpeg"）或绝对路径（.../bin/ffmpeg.exe），
    不能用字符串 replace 拼 ffprobe（会误伤路径目录名里的 "ffmpeg"），
    应基于目录 + 文件名替换。"""
    # 由 ffmpeg 路径推导 ffprobe：绝对路径则同名替换文件名，PATH 名则保持 PATH 查找
    if os.path.dirname(ffmpeg):
        base = os.path.basename(ffmpeg)
        ext = os.path.splitext(base)[1] or ".exe"
        ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + ext)
    else:
        ffprobe = "ffprobe"
    dur = _ffprobe_duration(media_path, ffprobe)
    if not dur or dur <= 0:
        return None
    if count <= 0:
        if quality == "low":
            count = max(6, min(12, int(dur / 480) + 1))
        elif quality == "high":
            count = max(12, min(40, int(dur / 180) + 1))
        else:  # smart
            count = max(8, min(24, int(dur / 300) + 1))
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


def _score_frame(path: os.PathLike) -> Optional[float]:
    """给抽出的帧打分，越高代表越适合做封面。
    综合考量：亮度适中、色彩丰富、宏观边缘结构、信息量高（避开黑场、纯色、花屏、过曝）。
    PIL 不可用时返回 None（调用方回退到单帧策略）。"""
    try:
        from PIL import Image, ImageStat
    except Exception:
        return None
    try:
        im = Image.open(path).convert("RGB")
        im.thumbnail((96, 96))  # 缩略打分，速度快
        # 轻微模糊，抑制像素级噪点，凸显宏观结构
        try:
            im = im.filter(__import__('PIL').ImageFilter.GaussianBlur(1.2))
        except Exception:
            pass
        gray = im.convert("L")
        # 亮度：过亮/过暗都扣分
        stat = ImageStat.Stat(gray)
        bright = stat.mean[0] / 255.0
        if bright < 0.05 or bright > 0.95:
            return 0.0  # 纯黑/纯白直接弃用
        b_score = 1.0 - abs(bright - 0.5) * 1.6

        # 色彩丰富度：像素级饱和度（max(R,G,B)-min(R,G,B)），灰度图趋近 0、彩色图较高
        w0, h0 = im.size
        rp, gch, bp = im.split()
        rp = rp.load(); gch = gch.load(); bp = bp.load()
        sat_sum = 0; sat_cnt = 0
        for y in range(0, h0, 2):
            for x in range(0, w0, 2):
                r = rp[x, y]; g2 = gch[x, y]; b = bp[x, y]
                mx = max(r, g2, b); mn = min(r, g2, b)
                sat_sum += (mx - mn)
                sat_cnt += 1
        color = (sat_sum / sat_cnt) / 255.0 if sat_cnt else 0.0
        c_score = min(1.0, color * 2.0)

        # 宏观边缘结构：用较大步长的差分（模糊后），抑制像素级噪点、保留真实轮廓
        w, h = gray.size
        gp = gray.load()
        step = max(4, min(w, h) // 24)  # 大步长采样宏观结构
        diff_sum = 0
        cnt = 0
        for y in range(0, h - step, step):
            for x in range(0, w - step, step):
                d = abs(gp[x, y] - gp[x + step, y]) + abs(gp[x, y] - gp[x, y + step])
                diff_sum += d
                cnt += 1
        edge = diff_sum / cnt / 510.0 if cnt else 0.0
        # 边缘过低=纯色/平滑；过高=花屏噪声；理想区间约 0.05~0.30
        if edge < 0.02:
            e_score = 0.0
        elif edge <= 0.30:
            e_score = edge / 0.30  # 0.02~0.30 线性到 1
        else:
            e_score = max(0.0, 1.0 - (edge - 0.30) * 3.0)  # 花屏/极端噪声重罚

        # 直方图熵：信息量
        hist = gray.histogram()
        total = sum(hist)
        ent = 0.0
        for c in hist:
            if c:
                p = c / total
                ent -= p * (p and _ln(p))
        ent_score = min(1.0, ent / 4.0)  # 8bit 熵最高约 8

        return b_score * 0.28 + c_score * 0.28 + e_score * 0.30 + ent_score * 0.14
    except Exception:
        return None


def _ln(x: float) -> float:
    try:
        import math
        return math.log(x)
    except Exception:
        return 0.0


def _extract_frame(video: str, t: float, ffmpeg: str, outp: os.PathLike) -> bool:
    """抽单帧：input seeking 优先（快），失败回退 output seeking（稳）。成功返回 True。"""
    cmds = [
        [ffmpeg, "-y", "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1", "-q:v", "3", str(outp)],
        [ffmpeg, "-y", "-i", video, "-ss", f"{t:.2f}", "-frames:v", "1", "-q:v", "3", str(outp)],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=True)
        except Exception:
            pass
        if os.path.exists(outp) and os.path.getsize(outp) >= 512:
            return True
        if os.path.exists(outp):
            try:
                os.unlink(outp)
            except OSError:
                pass
    return False


def extract_cover_from_video(conn: sqlite3.Connection, movie_id: int,
                             ffmpeg: str = "ffmpeg") -> Optional[str]:
    """从影片主视频中「智能」抽取一帧作为封面。成功返回 cover 文件名，失败返回 None。

    智能选帧：在中段范围（20%~80%）均匀采样多帧，逐帧打分（亮度/色彩/清晰度/信息量），
    挑出最能反映影片内容、且避开黑场/字幕/花屏/纯色的那一帧作封面。
    需要外部 ffmpeg（配置 ffmpeg_path 或 PATH；若都没有则自动探测 tools/ffmpeg）。"""
    from . import images
    from .config import COVER_DIR
    # 解析 ffmpeg：PATH 找不到时探测项目自带
    if not os.path.dirname(ffmpeg) and not shutil.which(ffmpeg):
        cand = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "tools", "ffmpeg", "bin", "ffmpeg.exe")
        if os.path.exists(cand):
            ffmpeg = cand
    video = movie_primary_file(conn, movie_id)
    if not video or not os.path.exists(video):
        return None
    # 推导 ffprobe
    if os.path.dirname(ffmpeg):
        base = os.path.basename(ffmpeg)
        ext = os.path.splitext(base)[1] or ".exe"
        ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + ext)
    else:
        ffprobe = "ffprobe"
    dur = _ffprobe_duration(video, ffprobe)
    if not dur or dur <= 0:
        return None
    row = query_one(conn, "SELECT key, cover, cover_source FROM movies WHERE id=?", (int(movie_id),))
    if not row:
        return None
    # 仅当「无封面」或「当前封面是视频抽帧」时才写入，避免覆盖正式刮削/上传封面
    if row["cover"] and row["cover_source"] not in ("", "video"):
        return "skipped"
    key = row["key"]
    outp = COVER_DIR / f"_extract_{int(movie_id)}_{os.getpid()}.jpg"

    # —— 智能选帧：中段采样 N 帧打分，选最佳 ——
    # 采样数：时长越长越能多采样，最多 14 帧；短影片少抽（抽帧开销也小）
    n = max(6, min(14, int(dur / 240) + 4))
    best_score = -1.0
    best_cand: Optional[os.PathLike] = None
    any_frame = False
    for i in range(n):
        # 时间点在中段 20%~85% 均匀分布，正片内容最集中的区域
        ratio = 0.20 + 0.65 * (i / max(1, n - 1))
        t = max(0.05, min(dur * ratio, dur * 0.9))
        if not _extract_frame(video, t, ffmpeg, outp):
            continue
        any_frame = True
        s = _score_frame(outp)
        if s is None:
            # PIL 不可用：放弃打分，直接用当前帧（保持兼容）
            name = images.save_local_file(key, outp)
            try:
                outp.unlink()
            except OSError:
                pass
            return name
        # 时间偏好：40%~65% 轻微加分（正片黄金区）
        time_bonus = 0.15 if 0.40 <= ratio <= 0.65 else 0.0
        score = s + time_bonus
        if score > best_score:
            best_score = score
            cand = COVER_DIR / f"_cand_{int(movie_id)}_{os.getpid()}.jpg"
            try:
                if os.path.exists(cand):
                    os.unlink(cand)
                import shutil as _sh
                _sh.copyfile(outp, cand)
                best_cand = cand
            except Exception:
                pass
    # 清理本轮中间抽帧
    try:
        if outp.exists():
            outp.unlink()
    except OSError:
        pass
    if not best_cand:
        return None
    # 迁移最优候选到正式封面文件
    name = images.save_local_file(key, best_cand)
    try:
        best_cand.unlink()
    except OSError:
        pass
    if not name:
        return None
    if name:
        conn.execute(
            "UPDATE movies SET cover = ?, cover_source = 'video' WHERE id = ?",
            (name, int(movie_id)),
        )
    return name


def get_previews(conn: sqlite3.Connection, movie_id: int) -> List[str]:
    row = conn.execute("SELECT paths FROM movie_previews WHERE movie_id=?", (movie_id,)).fetchone()
    return _parsej(row["paths"]) if row and row["paths"] else []


def get_previews_meta(conn: sqlite3.Connection, movie_id: int) -> Tuple[List[str], str]:
    """返回 (路径列表, 生成时的密度档位 quality)。无记录时 quality 返回 'smart'。"""
    row = conn.execute(
        "SELECT paths, quality FROM movie_previews WHERE movie_id=?", (movie_id,)).fetchone()
    if not row or not row["paths"]:
        return [], (row["quality"] if row else "smart")
    return _parsej(row["paths"]), (row["quality"] or "smart")


def set_previews(conn: sqlite3.Connection, movie_id: int, paths: List[str],
                 quality: str = "smart") -> None:
    conn.execute(
        "INSERT INTO movie_previews(movie_id, paths, quality) VALUES(?, ?, ?) "
        "ON CONFLICT(movie_id) DO UPDATE SET paths=excluded.paths, quality=excluded.quality, "
        "created_at=datetime('now','localtime')",
        (movie_id, _j(paths), quality),
    )


def year_in_review(conn: sqlite3.Connection, year: int) -> Dict[str, Any]:
    """返回某一年度的观影回顾数据，用于「年度回顾」页面。

    聚合维度：
      - 年度观看总览（时长、影片数、次数、天数）
      - 最长连续观看天数（streak）
      - 月度热力图（每月观看次数/时长）
      - 最爱厂商 / 女优 / 类型（按当年观看计数）
      - 年度新增到库的影片数（按 added_at 年份）
      - 评分分布（当年观看影片的 rating 分桶）
      - 状态分布（当年已看/未看/想看）
    """
    y = int(year)
    start = f"{y}-01-01 00:00:00"
    end = f"{y}-12-31 23:59:59"

    # 年度观看总览
    summ = query_one(conn, """
        SELECT COALESCE(SUM(s.watched_sec),0) AS total_sec,
               COUNT(DISTINCT s.movie_id) AS movies,
               COUNT(*) AS sessions,
               COUNT(DISTINCT DATE(s.started_at)) AS days
        FROM watch_sessions s
        WHERE s.started_at >= ? AND s.started_at <= ?
    """, (start, end)) or {}
    total_sec = float(summ.get("total_sec") or 0)
    movies = int(summ.get("movies") or 0)
    sessions = int(summ.get("sessions") or 0)
    days = int(summ.get("days") or 0)

    # 最长连续观看天数（按观看日期序列计算）
    dates = [r["d"] for r in query_all(conn, """
        SELECT DISTINCT DATE(s.started_at) AS d
        FROM watch_sessions s
        WHERE s.started_at >= ? AND s.started_at <= ?
        ORDER BY d
    """, (start, end))]
    streak = 0
    if dates:
        streak = 1
        cur = 1
        from datetime import datetime as _dt
        for i in range(1, len(dates)):
            a = _dt.strptime(dates[i - 1], "%Y-%m-%d").date()
            b = _dt.strptime(dates[i], "%Y-%m-%d").date()
            if (b - a).days == 1:
                cur += 1
                streak = max(streak, cur)
            else:
                cur = 1
    longest_streak = streak

    # 月度热力图
    months = []
    for m in range(1, 13):
        mm = f"{y}-{m:02d}"
        r = query_one(conn, """
            SELECT COUNT(*) AS cnt, COALESCE(SUM(s.watched_sec),0) AS sec
            FROM watch_sessions s
            WHERE s.started_at >= ? AND s.started_at < ?
        """, (f"{mm}-01 00:00:00", f"{y}-{m+1:02d}-01 00:00:00" if m < 12 else f"{y+1}-01-01 00:00:00"))
        months.append({
            "month": m,
            "count": int(r.get("cnt") or 0),
            "sec": float(r.get("sec") or 0),
        })

    # 最爱厂商 / 女优 / 类型（当年观看计数 top5）
    def top_by(sql, args):
        return [{"name": r["name"], "count": int(r["cnt"])} for r in query_all(conn, sql, args)]

    top_studios = top_by("""
        SELECT st.name AS name, COUNT(DISTINCT s.movie_id) AS cnt
        FROM watch_sessions s JOIN movies m ON m.id = s.movie_id
        LEFT JOIN studios st ON st.id = m.studio_id
        WHERE s.started_at >= ? AND s.started_at <= ? AND st.name IS NOT NULL
        GROUP BY st.id ORDER BY cnt DESC LIMIT 5
    """, (start, end))
    top_actresses = top_by("""
        SELECT a.name AS name, COUNT(DISTINCT s.movie_id) AS cnt
        FROM watch_sessions s JOIN movie_actress ma ON ma.movie_id = s.movie_id
        JOIN actresses a ON a.id = ma.actress_id
        WHERE s.started_at >= ? AND s.started_at <= ?
        GROUP BY a.id ORDER BY cnt DESC LIMIT 5
    """, (start, end))
    top_genres = top_by("""
        SELECT g.name AS name, COUNT(DISTINCT s.movie_id) AS cnt
        FROM watch_sessions s JOIN movie_genre mg ON mg.movie_id = s.movie_id
        JOIN genres g ON g.id = mg.genre_id
        WHERE s.started_at >= ? AND s.started_at <= ?
        GROUP BY g.id ORDER BY cnt DESC LIMIT 5
    """, (start, end))

    # 年度新增到库的影片数（按 created_at 年份）
    added = int(scalar(conn, """
        SELECT COUNT(*) FROM movies
        WHERE created_at >= ? AND created_at <= ?
    """, (start, end)) or 0)

    # 评分分布（当年观看影片的 rating 分桶 0/1/2/3/4/5）
    rating_rows = query_all(conn, """
        SELECT CAST(COALESCE(m.rating,0) AS INTEGER) AS bucket, COUNT(DISTINCT s.movie_id) AS cnt
        FROM watch_sessions s JOIN movies m ON m.id = s.movie_id
        WHERE s.started_at >= ? AND s.started_at <= ?
        GROUP BY bucket
    """, (start, end))
    rating_dist = {str(i): 0 for i in range(6)}
    for r in rating_rows:
        rating_dist[str(int(r["bucket"]))] = int(r["cnt"])

    # 状态分布（当年已看/未看/想看）
    status = query_one(conn, """
        SELECT
            SUM(CASE WHEN m.watched = 1 THEN 1 ELSE 0 END) AS watched,
            SUM(CASE WHEN m.watched = 0 THEN 1 ELSE 0 END) AS unwatched,
            SUM(CASE WHEN m.watchlist = 1 THEN 1 ELSE 0 END) AS watchlist
        FROM watch_sessions s JOIN movies m ON m.id = s.movie_id
        WHERE s.started_at >= ? AND s.started_at <= ?
    """, (start, end)) or {}
    status_dist = {
        "watched": int(status.get("watched") or 0),
        "unwatched": int(status.get("unwatched") or 0),
        "watchlist": int(status.get("watchlist") or 0),
    }

    # 年度最佳（当年观看中评分最高的几部）
    best = [{
        "id": r["id"], "code": r["code"], "title": r["title"],
        "cover": r["cover"], "rating": float(r["rating"] or 0),
    } for r in query_all(conn, """
        SELECT DISTINCT m.id, m.code, m.title, m.cover, m.rating
        FROM watch_sessions s JOIN movies m ON m.id = s.movie_id
        WHERE s.started_at >= ? AND s.started_at <= ? AND m.rating > 0
        ORDER BY m.rating DESC LIMIT 6
    """, (start, end))]

    return {
        "year": y,
        "summary": {
            "total_sec": total_sec,
            "movies": movies,
            "sessions": sessions,
            "days": days,
            "longest_streak": longest_streak,
            "added": added,
        },
        "months": months,
        "top_studios": top_studios,
        "top_actresses": top_actresses,
        "top_genres": top_genres,
        "rating_dist": rating_dist,
        "status_dist": status_dist,
        "best": best,
    }
