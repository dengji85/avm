import hashlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import load_config, roots_to_scan, video_exts
from .db import connect
from .jobs import SCAN
from .parser import parse_file


def iter_video_files(roots, exts):
    """用 os.scandir 递归枚举视频文件（比 os.walk 更快，直接拿 entry 信息）。"""
    for root in roots:
        if not os.path.isdir(root):
            continue
        stack = [root]
        while stack:
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                ext = Path(entry.name).suffix.lower().lstrip(".")
                                if ext in exts:
                                    yield entry.path
                        except OSError:
                            continue
            except OSError:
                continue


def _compute_quick_hash(path, size, sample=262144):
    """内容指纹：size + 头/尾各 sample 字节的 sha1 摘要成 64 位整数。
    不用全文件哈希，几万部也能秒级完成，足以识别真正重复的文件。"""
    if size == 0:
        return 0
    try:
        h = hashlib.sha1()
        h.update(str(size).encode())
        with open(path, "rb") as f:
            h.update(f.read(sample))
            if size > sample * 2:
                f.seek(-sample, 2)
                h.update(f.read(sample))
        digest = h.digest()[:8]
        val = int.from_bytes(digest, "big")
        # SQLite INTEGER 为有符号 64 位，需把无符号 64 位指纹折回有符号区间
        if val >= (1 << 63):
            val -= (1 << 64)
        return val
    except OSError:
        return 0


def quick_scan(progress_cb=None):
    cfg = load_config()
    exts = {e.lower().lstrip(".") for e in video_exts(cfg)}
    roots = roots_to_scan(cfg)
    total = 0
    for _ in iter_video_files(roots, exts):
        total += 1
    return total


def run_scan(progress_cb=None, incremental=True, workers=None, hash_files=False):
    """扫描媒体库。

    incremental : 跳过 size/mtime 均未变的文件（基于 movie_files 内存索引）
    workers     : 并发解析/算指纹的线程数（IO 与正则混合，默认 min(8, cpu)）
    hash_files  : 是否计算内容指纹（用于去重）。关闭可进一步提速

    并发策略：阶段一多线程并行「解析文件名 + 计算内容指纹」（无共享状态、
    无 DB 写）；阶段二由主线程串行 upsert 入库，保证同一番号的不同文件按序
    写入，彻底避免 UNIQUE(key) 竞态。
    """
    if progress_cb is None:
        progress_cb = SCAN.update
    cfg = load_config()
    exts = {e.lower().lstrip(".") for e in video_exts(cfg)}
    roots = roots_to_scan(cfg)
    if workers is None:
        workers = min(8, (os.cpu_count() or 4))
    sample = int(cfg.get("hash_sample_bytes", 262144))
    min_size_mb = float(cfg["library"].get("min_size_mb", 0) or 0)
    min_bytes = min_size_mb * 1024 * 1024

    SCAN.start()

    # 1) 内存索引，支撑增量跳过
    conn = connect()
    try:
        existing = {
            row["path"]: row
            for row in conn.execute(
                "SELECT id, movie_id, path, size, mtime, missing, quick_hash FROM movie_files"
            )
        }
    finally:
        conn.close()

    to_process, alive, enumerated, skipped = [], set(), 0, 0
    SCAN.update(phase="enumerating", total=0, done=0, current="正在遍历目录…")
    for path in iter_video_files(roots, exts):
        try:
            st = os.stat(path)
        except OSError:
            continue
        size, mtime = st.st_size, st.st_mtime
        if size < min_bytes:
            continue
        enumerated += 1
        rec = existing.get(path)
        if incremental and rec and not rec["missing"] \
                and abs(float(rec["size"] or 0) - size) < 0.5 \
                and abs(float(rec["mtime"] or 0) - mtime) < 1:
            alive.add(path)
            skipped += 1
            SCAN.update(skipped=skipped, total=enumerated,
                        current=f"正在遍历目录（已发现 {enumerated} 个视频）")
            continue
        to_process.append((path, size, mtime))
        alive.add(path)
        SCAN.update(total=enumerated,
                    current=f"正在遍历目录（已发现 {enumerated} 个视频）")

    # 2) 标记已从磁盘消失的文件（仅在本次扫描的 root 集合内判定）
    removed = set(existing.keys()) - alive
    if removed:
        c2 = connect()
        try:
            c2.executemany("UPDATE movie_files SET missing = 1 WHERE path = ?",
                           [(p,) for p in removed])
            c2.commit()
        finally:
            c2.close()

    SCAN.update(phase="scanning", total=len(to_process), done=0, pending=len(to_process))

    # 3) 阶段一：并发解析 + 计算指纹（无 DB 写）
    def worker(item):
        path, size, mtime = item
        parsed = parse_file(path)
        qh = _compute_quick_hash(path, size, sample) if hash_files else 0
        return {"parsed": parsed, "path": path, "size": size, "mtime": mtime, "qh": qh}

    parsed_items = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(worker, it) for it in to_process]
        done = 0
        for fut in as_completed(futs):
            parsed_items.append(fut.result())
            done += 1
            SCAN.update(scanned=done, done=done)

    # 4) 阶段二：主线程串行写库，避免同番号竞态
    added = updated = unchanged = errors = 0
    conn = connect()
    try:
        for r in parsed_items:
            try:
                act = store_upsert(conn, r["parsed"], r["path"], r["size"], r["mtime"], r["qh"])
                conn.commit()
                if act == "added":
                    added += 1
                elif act == "updated":
                    updated += 1
                else:
                    unchanged += 1
            except Exception:
                errors += 1
            SCAN.update(added=added, updated=updated, scanned=len(parsed_items), errors=errors)
    finally:
        conn.close()

    # 5) 封面嗅探（单点执行，避免多线程竞争同一 movie 的封面）
    batch_local_covers(progress_cb)

    SCAN.finish(
        "扫描完成：新增 %d / 更新 %d / 跳过 %d / 缺失 %d"
        % (added, updated, skipped, len(removed))
    )
    return {"total": enumerated, "added": added, "updated": updated,
            "unchanged": unchanged, "missing": len(removed), "errors": errors}


def store_upsert(conn, parsed, path, size, mtime, qh):
    # 延迟导入，避免循环依赖
    from . import store
    return store.upsert_scanned_file(conn, parsed, path, size, mtime, quick_hash=qh)


def batch_local_covers(progress_cb=None):
    if progress_cb is None:
        progress_cb = SCAN.update
    from . import scraper
    cfg = load_config()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT m.id FROM movies m "
            "WHERE (m.cover IS NULL OR m.cover = '') "
            "AND EXISTS (SELECT 1 FROM movie_files f WHERE f.movie_id = m.id AND f.missing = 0)"
        ).fetchall()
        ids = [r["id"] for r in rows]
    finally:
        conn.close()
    total = len(ids)
    done = 0
    for mid in ids:
        try:
            scraper.sniff_local_cover(conn, mid, cfg)
        except Exception:
            pass
        done += 1
        if done % 20 == 0:
            progress_cb(cover_done=done, cover_total=total)
    progress_cb(cover_done=total, cover_total=total)


def remove_missing():
    """清理已被标记为 missing 的文件记录，并删除因此失去所有文件的影片。"""
    from . import store
    conn = connect()
    try:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM movie_files WHERE missing = 1").fetchall()]
        for fid in ids:
            conn.execute("DELETE FROM movie_files WHERE id = ?", (fid,))
        orphan = conn.execute(
            "SELECT m.id FROM movies m WHERE NOT EXISTS "
            "(SELECT 1 FROM movie_files f WHERE f.movie_id = m.id)").fetchall()
        for r in orphan:
            store.delete_movie(conn, r["id"])
        conn.commit()
        return len(ids), len(orphan)
    finally:
        conn.close()


def preview_parse(names):
    """供前端「文件名解析预览」使用：返回每个输入名的解析结果（纯文件名解析，不访问磁盘）。"""
    out = []
    for nm in names:
        nm = str(nm)
        try:
            r = parse_file(nm)
        except Exception:
            r = {"has_code": 0, "code": "", "code_rule": "", "part": 1, "subtitle": 0, "uncensored": 0}
        out.append({
            "input": nm,
            "matched": bool(r.get("has_code")),
            "code": r.get("code") or "",
            "rule": r.get("code_rule") or "",
            "part": r.get("part") or 1,
            "subtitle": bool(r.get("subtitle")),
            "uncensored": bool(r.get("uncensored")),
        })
    return out
