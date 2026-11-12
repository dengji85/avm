"""精彩时刻标记：一部片可以有多段「好看/值得回看」的区间。

播放器把这些区间画在进度条上，点一下跳到起点；片头/片尾跳过（movies.skip_intro /
skip_outro）仍然是整片级的单点设置，两者互不影响。

参考 Stash 的 SceneMarker：title、seconds（起点）、可选的 end_seconds（区间）、
主标记（唯一）、标签（共用 tags 词表）与自动生成的截图。这里在其上做了取舍：
区间是我们的一等公民（Stash 的 end_seconds 只是可选），主标记是「唯一」而不是
「必选的主标签」（本项目的卡片预览不走标记截图）。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from .. import store
from ..config import COVER_DIR, markers_cfg
from ..db import db, query_all, query_one
from ..ext_components import ffmpeg_exe
from ..threadpool import submit_task

router = APIRouter()

MAX_MARKERS = 50                 # 单片上限：再多进度条也画不下，也没人看得清
MAX_LABEL = 40                   # 标签长度上限（字符）
MAX_SPAN = 30 * 60               # 单段最长 30 分钟：防止把整部片标成一段
MAX_TAGS = 8                     # 单个标记的标签数上限
MAX_TAG = 20                     # 单个标签长度上限
MAX_PAGE_SIZE = 100              # 标记墙每页上限
MAX_FILTER_TAGS = 16             # 标记墙一次最多按 16 个标签筛
_BACKFILL_PER_PAGE = 4           # 标记墙每次列出最多补 4 条素材（避免刷一次就排一串 ffmpeg）
PREVIEW_SECONDS = 4              # 预览短片时长：3–5 秒足够看清这一段，循环播放不突兀
PREVIEW_WIDTH = 480              # 预览短片宽度上限（手机上也不至于糊）
KINDS = ("marker", "favorite")
_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_TAG_SPLIT = re.compile(r"[,，、;；]")

_SELECT = ("SELECT id, movie_id, start_sec, end_sec, label, kind, color, sort, "
           "is_primary, thumb, preview, created_at FROM movie_markers")


def _public(row: Any, tags: List[str] = None) -> Dict[str, Any]:
    return {
        "id": int(row["id"]),
        "movie_id": int(row["movie_id"]),
        "start": round(float(row["start_sec"] or 0), 3),
        "end": round(float(row["end_sec"] or 0), 3),
        "label": row["label"] or "",
        "kind": row["kind"] or "marker",
        "color": row["color"] or "",
        "primary": bool(row["is_primary"]),
        "thumb": row["thumb"] or "",
        "preview": row["preview"] or "",
        "tags": tags or [],
        "created_at": row["created_at"] or "",
    }


def _sec(payload: Dict[str, Any], key: str) -> float:
    try:
        val = float(payload.get(key))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="起止时间必须是秒数")
    if val < 0:
        raise HTTPException(status_code=400, detail="起止时间不能为负数")
    return round(val, 3)


def _movie_duration(conn, movie_id: int) -> float:
    """影片时长（秒）：优先用实际播放记录的时长，其次用刮削到的片长（分钟）。"""
    row = query_one(conn, "SELECT duration FROM watch_progress WHERE movie_id = ?", (movie_id,))
    dur = float((row or {}).get("duration") or 0)
    if dur > 0:
        return dur
    row = query_one(conn, "SELECT runtime FROM movies WHERE id = ?", (movie_id,))
    try:
        return max(0.0, float((row or {}).get("runtime") or 0) * 60)
    except (TypeError, ValueError):
        return 0.0


def _clean_label(value: Any) -> str:
    label = str(value or "").strip()
    if len(label) > MAX_LABEL:
        raise HTTPException(status_code=400, detail="标签最多 %d 个字" % MAX_LABEL)
    return label


def _clean_kind(value: Any) -> str:
    kind = str(value or "marker").strip().lower() or "marker"
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail="类型只能是 marker 或 favorite")
    return kind


def _clean_color(value: Any) -> str:
    color = str(value or "").strip()
    if color and not _COLOR_RE.match(color):
        raise HTTPException(status_code=400, detail="颜色格式应为 #rgb 或 #rrggbb")
    return color


def _clean_tags(value: Any) -> List[str]:
    """接受列表或「逗号 / 全角逗号 / 顿号 / 分号」分隔的字符串，去重保序。"""
    if value is None:
        return []
    if isinstance(value, str):
        parts = _TAG_SPLIT.split(value)
    elif isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        raise HTTPException(status_code=400, detail="标签格式不正确")
    out: List[str] = []
    seen = set()
    for raw in parts:
        name = str(raw or "").strip()
        if not name:
            continue
        if len(name) > MAX_TAG:
            raise HTTPException(status_code=400, detail="单个标签最多 %d 个字" % MAX_TAG)
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    if len(out) > MAX_TAGS:
        raise HTTPException(status_code=400, detail="一个标记最多 %d 个标签" % MAX_TAGS)
    return out


def _set_tags(conn, marker_id: int, names: List[str]) -> None:
    conn.execute("DELETE FROM movie_marker_tag WHERE marker_id = ?", (marker_id,))
    for name in names:
        tid = store.ensure_tag(conn, name)
        if tid:
            conn.execute("INSERT OR IGNORE INTO movie_marker_tag(marker_id, tag_id) VALUES(?, ?)",
                         (marker_id, tid))


def _tags_of(conn, movie_id: int) -> Dict[int, List[str]]:
    rows = query_all(
        conn,
        "SELECT mt.marker_id AS mid, t.name AS name FROM movie_marker_tag mt "
        "JOIN tags t ON t.id = mt.tag_id "
        "JOIN movie_markers m ON m.id = mt.marker_id "
        "WHERE m.movie_id = ? ORDER BY t.name", (movie_id,))
    out: Dict[int, List[str]] = {}
    for r in rows:
        out.setdefault(int(r["mid"]), []).append(r["name"])
    return out


def _tags_of_markers(conn, ids: List[int]) -> Dict[int, List[str]]:
    """一次查出多条标记的标签（标记墙一页混着多部影片，不能按 movie_id 查）。"""
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    rows = query_all(
        conn,
        "SELECT mt.marker_id AS mid, t.name AS name FROM movie_marker_tag mt "
        "JOIN tags t ON t.id = mt.tag_id WHERE mt.marker_id IN (" + ph + ") "
        "ORDER BY t.name", tuple(ids))
    out: Dict[int, List[str]] = {}
    for r in rows:
        out.setdefault(int(r["mid"]), []).append(r["name"])
    return out


def _validate(conn, movie_id: int, start: float, end: float) -> None:
    if end <= start:
        raise HTTPException(status_code=400, detail="结束时间必须大于开始时间")
    if end - start > MAX_SPAN:
        raise HTTPException(status_code=400, detail="单段不能超过 30 分钟")
    total = _movie_duration(conn, movie_id)
    if total > 0 and end > total + 1:
        raise HTTPException(
            status_code=400,
            detail="结束时间超出影片时长（约 %d 分 %d 秒）"
                   % (int(total // 60), int(total % 60)))


def _local_source(movie_id: int) -> str:
    """该片第一个可播的本地文件；在线 / 串流片返回空串（不生成素材，也不报错）。"""
    with db() as conn:
        row = query_one(
            conn,
            "SELECT path FROM movie_files WHERE movie_id = ? "
            "AND COALESCE(missing,0) = 0 AND COALESCE(kind,'local') <> 'online' "
            "AND COALESCE(strm_url,'') = '' ORDER BY part LIMIT 1", (movie_id,))
    src = str((row or {}).get("path") or "")
    return src if src and os.path.exists(src) else ""


def _make_thumb(movie_id: int, marker_id: int, seconds: float) -> None:
    """后台抽一帧做标记缩略图；任何失败都静默（打点本身已经成功）。"""
    try:
        src = _local_source(movie_id)
        if not src:
            return  # 在线 / 串流片没有本地文件，留空占位，不报错
        out = COVER_DIR / "marker" / str(movie_id) / ("%d.jpg" % marker_id)
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [ffmpeg_exe(), "-y", "-ss", "%.2f" % max(0.0, seconds), "-i", src,
             "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "4", str(out)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
        if out.exists():
            with db() as conn:
                conn.execute("UPDATE movie_markers SET thumb = ? WHERE id = ? AND movie_id = ?",
                             ("marker/%d/%d.jpg" % (movie_id, marker_id), marker_id, movie_id))
    except Exception:
        pass


def _thumb_path(movie_id: int, marker_id: int):
    return COVER_DIR / "marker" / str(movie_id) / ("%d.jpg" % marker_id)


def _preview_path(movie_id: int, marker_id: int):
    return COVER_DIR / "marker" / str(movie_id) / ("%d.mp4" % marker_id)


def _make_preview(movie_id: int, marker_id: int, seconds: float) -> None:
    """后台生成 3–5 秒静音循环短片（H.264 / 无音轨），供卡片悬停播放。

    只处理本地文件；关掉配置或任何一步失败都静默——预览是锦上添花，不影响标记本身。
    """
    try:
        if not markers_cfg().get("preview"):
            return
        src = _local_source(movie_id)
        if not src:
            return
        out = _preview_path(movie_id, marker_id)
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [ffmpeg_exe(), "-y", "-ss", "%.2f" % max(0.0, seconds),
             "-t", str(PREVIEW_SECONDS), "-i", src,
             "-an", "-vf", "scale=%d:-2" % PREVIEW_WIDTH,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)
        if out.exists() and out.stat().st_size > 0:
            with db() as conn:
                conn.execute("UPDATE movie_markers SET preview = ? WHERE id = ? AND movie_id = ?",
                             ("marker/%d/%d.mp4" % (movie_id, marker_id), marker_id, movie_id))
            _prune_previews()
    except Exception:
        pass


def _prune_previews() -> None:
    """预览短片按总占用上限做 LRU 清理：最久未播放（mtime 最旧）的先删。

    播放接口下发 mp4 时会 touch 一次 mtime，因此「最近看过」的片段不会被清掉。
    清理同时把 DB 里的 preview 置空，前端自然回退到静态缩略图。
    """
    try:
        limit_mb = int(markers_cfg().get("preview_limit_mb") or 0)
    except (TypeError, ValueError):
        limit_mb = 0
    root = COVER_DIR / "marker"
    if limit_mb <= 0 or not root.exists():
        return
    try:
        files = sorted(root.glob("*/*.mp4"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    sizes: Dict[Any, int] = {}
    total = 0
    for p in files:
        try:
            sizes[p] = p.stat().st_size
            total += sizes[p]
        except OSError:
            continue
    limit = limit_mb * 1024 * 1024
    removed: List[str] = []
    for p in files:
        if total <= limit:
            break
        try:
            p.unlink()
            total -= sizes.get(p, 0)
            removed.append(p.relative_to(COVER_DIR).as_posix())
        except (OSError, ValueError):
            continue
    if not removed:
        return
    try:
        with db() as conn:
            for rel in removed:
                conn.execute("UPDATE movie_markers SET preview = '' WHERE preview = ?", (rel,))
    except Exception:
        pass


_PENDING: set = set()            # 正在生成素材（缩略图 / 预览）的标记 id，防止重复排队
_PENDING_LOCK = threading.Lock()


def _queue_assets(movie_id: int, marker_id: int, seconds: float, force: bool = False) -> None:
    """排队生成素材；已在队列里就不重复排（2 秒后的刷新会触发补素材）。"""
    with _PENDING_LOCK:
        if marker_id in _PENDING:
            return
        _PENDING.add(marker_id)
    submit_task(_fill_assets, movie_id, marker_id, seconds, force)


def _need_assets(row: Any) -> bool:
    """缺缩略图，或开着预览却还没有短片 → 值得补一次。"""
    if not (row["thumb"] or ""):
        return True
    return bool(markers_cfg().get("preview")) and not (row["preview"] or "")


def _backfill_thumbs(conn, movie_id: int, rows: List[Any]) -> List[tuple]:
    """老标记（功能上线前建的）第一次列出时顺手补素材，每次最多补 3 条。

    只做「挑选 + 占位」，真正排队由调用方在事务结束后做，避免读写连接相互等锁。
    """
    missing = [r for r in rows if _need_assets(r)]
    if not missing:
        return []
    has_local = query_one(
        conn,
        "SELECT 1 x FROM movie_files WHERE movie_id = ? AND COALESCE(missing,0) = 0 "
        "AND COALESCE(kind,'local') <> 'online' AND COALESCE(strm_url,'') = '' LIMIT 1",
        (movie_id,))
    if not has_local:
        return []
    todo = []
    with _PENDING_LOCK:
        for r in missing:
            mid = int(r["id"])
            if mid in _PENDING:
                continue
            _PENDING.add(mid)
            todo.append((mid, float(r["start_sec"] or 0)))
            if len(todo) >= 3:
                break
    return todo


def _fill_assets(movie_id: int, marker_id: int, seconds: float, force: bool = False) -> None:
    """补缩略图 / 预览短片：只生成缺的那份；改起点（force）时两份都重生成。"""
    try:
        with db() as conn:
            row = query_one(
                conn, "SELECT thumb, preview FROM movie_markers WHERE id = ? AND movie_id = ?",
                (marker_id, movie_id))
        if not row:
            return
        if force or not (row["thumb"] or ""):
            _make_thumb(movie_id, marker_id, seconds)
        if force or not (row["preview"] or ""):
            _make_preview(movie_id, marker_id, seconds)
    finally:
        with _PENDING_LOCK:
            _PENDING.discard(marker_id)


def _backfill_page(conn, rows: List[Any]) -> List[tuple]:
    """标记墙：一页里挑最多 4 条缺图的补，只挑有本地文件的影片。

    与 ``_backfill_thumbs`` 同样的分工——事务内只挑选占位，排队由调用方在事务外做。
    """
    todo: List[tuple] = []
    local_cache: Dict[int, bool] = {}
    with _PENDING_LOCK:
        for r in rows:
            if not _need_assets(r):
                continue
            mid = int(r["id"])
            if mid in _PENDING:
                continue
            mv = int(r["movie_id"])
            if mv not in local_cache:
                local_cache[mv] = bool(query_one(
                    conn,
                    "SELECT 1 x FROM movie_files WHERE movie_id = ? AND COALESCE(missing,0) = 0 "
                    "AND COALESCE(kind,'local') <> 'online' AND COALESCE(strm_url,'') = '' LIMIT 1",
                    (mv,)))
            if not local_cache[mv]:
                continue
            _PENDING.add(mid)
            todo.append((mv, mid, float(r["start_sec"] or 0)))
            if len(todo) >= _BACKFILL_PER_PAGE:
                break
    return todo


@router.get("/movies/{movie_id}/markers")
def list_markers(movie_id: int) -> Dict[str, Any]:
    with db() as conn:
        if not query_one(conn, "SELECT id FROM movies WHERE id = ?", (movie_id,)):
            raise HTTPException(status_code=404, detail="影片不存在")
        rows = query_all(conn, _SELECT + " WHERE movie_id = ? ORDER BY start_sec, sort, id",
                         (movie_id,))
        tags = _tags_of(conn, movie_id)
        todo = _backfill_thumbs(conn, movie_id, rows)
    for mid, sec in todo:
        submit_task(_fill_assets, movie_id, mid, sec)
    return {"ok": True, "max": MAX_MARKERS,
            "items": [_public(r, tags.get(int(r["id"]), [])) for r in rows]}


@router.get("/markers")
def list_all_markers(page: int = 1, page_size: int = 24, tag: str = "",
                     movie_id: int = 0) -> Dict[str, Any]:
    """标记墙：跨影片列出标记，可按标签 / 影片筛选（归档影片不出现在墙上）。"""
    page = max(1, int(page or 1))
    size = min(MAX_PAGE_SIZE, max(1, int(page_size or 24)))
    names: List[str] = []
    seen = set()
    for raw in _TAG_SPLIT.split(tag or ""):
        name = raw.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    names = names[:MAX_FILTER_TAGS]

    where = ["COALESCE(mv.archived_at,'') = ''"]
    params: List[Any] = []
    if movie_id > 0:
        where.append("mk.movie_id = ?")
        params.append(movie_id)
    if names:
        where.append("EXISTS (SELECT 1 FROM movie_marker_tag mt JOIN tags t ON t.id = mt.tag_id "
                     "WHERE mt.marker_id = mk.id AND t.name IN ("
                     + ",".join("?" * len(names)) + "))")
        params.extend(names)
    cond = " AND ".join(where)

    with db() as conn:
        total = int(query_one(
            conn,
            "SELECT COUNT(*) c FROM movie_markers mk JOIN movies mv ON mv.id = mk.movie_id "
            "WHERE " + cond, tuple(params))["c"])
        rows = query_all(
            conn,
            "SELECT mk.id, mk.movie_id, mk.start_sec, mk.end_sec, mk.label, mk.kind, mk.color, "
            "mk.sort, mk.is_primary, mk.thumb, mk.preview, mk.created_at, "
            "mv.code AS mv_code, mv.title AS mv_title "
            "FROM movie_markers mk JOIN movies mv ON mv.id = mk.movie_id "
            "WHERE " + cond + " ORDER BY mk.created_at DESC, mk.id DESC LIMIT ? OFFSET ?",
            tuple(params) + (size, (page - 1) * size))
        tags = _tags_of_markers(conn, [int(r["id"]) for r in rows])
        # 标签分面：只看当前影片范围，不受已选标签影响，否则选完只剩一个选项
        fwhere = ["COALESCE(mv.archived_at,'') = ''"]
        fparams: List[Any] = []
        if movie_id > 0:
            fwhere.append("mk.movie_id = ?")
            fparams.append(movie_id)
        facets = query_all(
            conn,
            "SELECT t.name AS name, COUNT(*) AS n FROM movie_marker_tag mt "
            "JOIN tags t ON t.id = mt.tag_id "
            "JOIN movie_markers mk ON mk.id = mt.marker_id "
            "JOIN movies mv ON mv.id = mk.movie_id WHERE " + " AND ".join(fwhere) +
            " GROUP BY t.name ORDER BY n DESC, t.name LIMIT 40", tuple(fparams))
        todo = _backfill_page(conn, rows)
    for mid_movie, mid, sec in todo:
        submit_task(_fill_assets, mid_movie, mid, sec)
    items = []
    for r in rows:
        item = _public(r, tags.get(int(r["id"]), []))
        item["movie"] = {"id": int(r["movie_id"]), "code": r["mv_code"] or "",
                         "title": r["mv_title"] or ""}
        items.append(item)
    return {"ok": True, "total": total, "page": page, "page_size": size,
            "selected_tags": names, "items": items,
            "tags": [{"name": r["name"], "count": int(r["n"])} for r in facets]}


@router.post("/movies/{movie_id}/markers")
def add_marker(movie_id: int, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    start = _sec(payload, "start")
    end = _sec(payload, "end")
    label = _clean_label(payload.get("label"))
    kind = _clean_kind(payload.get("kind"))
    color = _clean_color(payload.get("color"))
    tag_names = _clean_tags(payload.get("tags"))
    with db() as conn:
        if not query_one(conn, "SELECT id FROM movies WHERE id = ?", (movie_id,)):
            raise HTTPException(status_code=404, detail="影片不存在")
        _validate(conn, movie_id, start, end)
        count = query_one(conn, "SELECT COUNT(*) c FROM movie_markers WHERE movie_id = ?",
                          (movie_id,))["c"]
        if count >= MAX_MARKERS:
            raise HTTPException(status_code=400,
                                detail="最多只能标记 %d 段，请先删掉不用的" % MAX_MARKERS)
        # 第一条自动成为主标记，用户之后可用 ★ 切换
        primary = 1 if (bool(payload.get("is_primary")) or count == 0) else 0
        if primary:
            conn.execute("UPDATE movie_markers SET is_primary = 0 WHERE movie_id = ?", (movie_id,))
        cur = conn.execute(
            "INSERT INTO movie_markers(movie_id, start_sec, end_sec, label, kind, color, sort, "
            "is_primary) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (movie_id, start, end, label, kind, color, count, primary))
        mid = int(cur.lastrowid)
        _set_tags(conn, mid, tag_names)
        row = query_one(conn, _SELECT + " WHERE id = ?", (mid,))
    _queue_assets(movie_id, mid, start)
    return {"ok": True, "item": _public(row, tag_names)}


@router.put("/movies/{movie_id}/markers/{marker_id}")
def update_marker(movie_id: int, marker_id: int,
                  payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    with db() as conn:
        row = query_one(conn, _SELECT + " WHERE id = ? AND movie_id = ?", (marker_id, movie_id))
        if not row:
            raise HTTPException(status_code=404, detail="标记不存在")
        old_start = float(row["start_sec"] or 0)
        start = _sec(payload, "start") if "start" in payload else old_start
        end = _sec(payload, "end") if "end" in payload else float(row["end_sec"] or 0)
        label = _clean_label(payload.get("label")) if "label" in payload else (row["label"] or "")
        kind = _clean_kind(payload.get("kind")) if "kind" in payload else (row["kind"] or "marker")
        color = _clean_color(payload.get("color")) if "color" in payload else (row["color"] or "")
        _validate(conn, movie_id, start, end)
        if "is_primary" in payload:
            # 主标记唯一：先清掉同片旧值，再写本行（部分唯一索引兜底）
            conn.execute("UPDATE movie_markers SET is_primary = 0 WHERE movie_id = ?", (movie_id,))
        conn.execute(
            "UPDATE movie_markers SET start_sec = ?, end_sec = ?, label = ?, kind = ?, color = ? "
            "WHERE id = ? AND movie_id = ?",
            (start, end, label, kind, color, marker_id, movie_id))
        if "is_primary" in payload:
            conn.execute("UPDATE movie_markers SET is_primary = ? WHERE id = ? AND movie_id = ?",
                         (1 if payload.get("is_primary") else 0, marker_id, movie_id))
        tag_names = None
        if "tags" in payload:
            tag_names = _clean_tags(payload.get("tags"))
            _set_tags(conn, marker_id, tag_names)
        newrow = query_one(conn, _SELECT + " WHERE id = ?", (marker_id,))
        tags = _tags_of(conn, movie_id)
        item = _public(newrow, tag_names if tag_names is not None else tags.get(marker_id, []))
    # 起点变了才重抽缩略图（只改标签 / 主标记不必再跑一次 ffmpeg）
    if "start" in payload and abs(start - old_start) > 1e-6:
        _queue_assets(movie_id, marker_id, start, True)
    return {"ok": True, "item": item}


@router.delete("/movies/{movie_id}/markers/{marker_id}")
def delete_marker(movie_id: int, marker_id: int) -> Dict[str, Any]:
    with db() as conn:
        row = query_one(conn, "SELECT is_primary FROM movie_markers WHERE id = ? AND movie_id = ?",
                        (marker_id, movie_id))
        if not row:
            raise HTTPException(status_code=404, detail="标记不存在")
        conn.execute("DELETE FROM movie_marker_tag WHERE marker_id = ?", (marker_id,))
        conn.execute("DELETE FROM movie_markers WHERE id = ? AND movie_id = ?",
                     (marker_id, movie_id))
        if row["is_primary"]:
            # 删掉主标记后，把最早的一条扶正，避免进度条上没有加亮的那个
            nxt = query_one(conn, "SELECT id FROM movie_markers WHERE movie_id = ? "
                                  "ORDER BY start_sec, sort, id LIMIT 1", (movie_id,))
            if nxt:
                conn.execute("UPDATE movie_markers SET is_primary = 1 WHERE id = ?",
                             (int(nxt["id"]),))
    for p in (_thumb_path(movie_id, marker_id), _preview_path(movie_id, marker_id)):
        try:
            p.unlink()
        except OSError:
            pass
    return {"ok": True}


@router.get("/covers/marker/{movie_id}/{fname}")
def marker_thumb(movie_id: int, fname: str) -> FileResponse:
    """标记素材：缩略图 <id>.jpg / 预览短片 <id>.mp4，都在 data/covers/marker/<movie_id>/。"""
    if os.path.basename(fname) != fname:
        raise HTTPException(404, "素材不存在")
    p = COVER_DIR / "marker" / str(movie_id) / fname
    if not p.exists():
        raise HTTPException(404, "素材不存在")
    if p.suffix.lower() == ".mp4":
        # 触碰 mtime = 记录「最近播放过」，LRU 清理时优先保留看过的片段
        try:
            os.utime(p, None)
        except OSError:
            pass
        return FileResponse(str(p), media_type="video/mp4")
    return FileResponse(str(p), media_type="image/jpeg")
