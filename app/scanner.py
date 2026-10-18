import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import subprobe
from .config import load_config, roots_to_scan, video_exts
from .db import connect
from .jobs import SCAN
from .parser import parse_file


def iter_video_files(roots, exts, ignore_suffixes=()):
    """用 os.scandir 递归枚举视频文件（比 os.walk 更快，直接拿 entry 信息）。

    ignore_suffixes：下载器临时/未完成文件后缀，命中直接跳过，避免半成品入库。
    """
    ignores = tuple(str(s).lower() for s in (ignore_suffixes or ()))
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
                                low = entry.name.lower()
                                if ignores and low.endswith(ignores):
                                    continue
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


STRM_MAX_BYTES = 8192


def read_strm_target(path: str) -> str:
    """读取 ``.strm`` 里的目标地址（约定是一行 http/https 链接，可指向 mp4 / m3u8）。

    返回空串表示这个 .strm 不可用（空文件 / 不是网络地址），扫描时直接跳过，
    避免往库里塞一条永远放不出来的记录。只读前 8KB，防止误把大文件当 strm 读。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(20):
                line = f.readline(STRM_MAX_BYTES)
                if not line:
                    break
                s = line.strip().lstrip("\ufeff")
                if not s or s.startswith("#"):
                    continue
                return s if s.lower().startswith(("http://", "https://")) else ""
    except OSError:
        return ""
    return ""


def quick_scan(progress_cb=None):
    cfg = load_config()
    exts = {e.lower().lstrip(".") for e in video_exts(cfg)}
    roots = roots_to_scan(cfg)
    ignores = cfg.get("library", {}).get("ignore_suffixes") or []
    total = 0
    for _ in iter_video_files(roots, exts, ignores):
        total += 1
    return total


def run_scan(progress_cb=None, incremental=True, workers=None, hash_files=False, auto_cleanup=None):
    """扫描媒体库。

    incremental : 跳过 size/mtime 均未变的文件（基于 movie_files 内存索引）
    workers     : 并发解析/算指纹的线程数（IO 与正则混合，默认 min(8, cpu)）
    hash_files  : 是否计算内容指纹（用于去重）。关闭可进一步提速
    auto_cleanup: 扫描结束是否自动清理失效记录（缺文件/空影片/孤儿元数据）。
                  缺省读 config.library.auto_cleanup（默认开启）。

    并发策略：阶段一多线程并行「解析文件名 + 计算内容指纹」（无共享状态、
    无 DB 写）；阶段二由主线程串行 upsert 入库，保证同一番号的不同文件按序
    写入，彻底避免 UNIQUE(key) 竞态。阶段六自动清理，保证库与磁盘一致。
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
    # 内嵌字幕探测：默认开启；只对本地视频跑，.strm 没有文件可探
    probe_subs = bool(cfg.get("library", {}).get("probe_subtitles", True))

    SCAN.start()

    # 1) 内存索引，支撑增量跳过
    conn = connect()
    try:
        existing = {
            row["path"]: row
            for row in conn.execute(
                "SELECT id, movie_id, path, size, mtime, missing, quick_hash, strm_url, "
                "sub_tracks, sub_probe_at FROM movie_files"
            )
        }
    finally:
        conn.close()

    to_process, alive, enumerated, skipped = [], set(), 0, 0
    ignores = cfg.get("library", {}).get("ignore_suffixes") or []
    SCAN.update(phase="enumerating", total=0, done=0, current="正在遍历目录…")
    for path in iter_video_files(roots, exts, ignores):
        try:
            st = os.stat(path)
        except OSError:
            continue
        size, mtime = st.st_size, st.st_mtime
        is_strm = path.lower().endswith(".strm")
        # .strm 本身只有一行 URL、体积几十字节，不能按「最小体积」过滤掉
        if is_strm:
            if not read_strm_target(path):
                continue
        elif size < min_bytes:
            continue
        enumerated += 1
        rec = existing.get(path)
        # 旧的 .strm 记录还没解析出地址时，即使 size/mtime 没变也要走一遍
        stale_strm = bool(is_strm and rec and not (rec["strm_url"] or ""))
        # 老库升级后还没有字幕探测结果的文件：即使 size/mtime 没变也要补测一次
        stale_sub = bool(probe_subs and not is_strm and rec and not (rec["sub_probe_at"] or ""))
        if incremental and rec and not rec["missing"] and not stale_strm and not stale_sub \
                and abs(float(rec["size"] or 0) - size) < 0.5 \
                and abs(float(rec["mtime"] or 0) - mtime) < 1:
            alive.add(path)
            skipped += 1
            SCAN.update(skipped=skipped, total=enumerated,
                        current=f"正在遍历目录（已发现 {enumerated} 个视频）")
            continue
        to_process.append((path, size, mtime, rec))
        alive.add(path)
        SCAN.update(total=enumerated,
                    current=f"正在遍历目录（已发现 {enumerated} 个视频）")

    # 2) 标记已从磁盘消失的文件：只在「本次扫描的 root 集合内」判定。
    #    原实现直接 set(existing) - alive，会把未参与本次扫描的目录（例如
    #    未挂载的磁盘、临时移出的库路径）里的文件整体误标为缺失。
    lowered_roots = [r.replace("\\", "/").lower().rstrip("/") for r in roots]
    removed = set()
    for p in existing.keys():
        lp = p.replace("\\", "/").lower()
        if not any(lp.startswith(r + "/") or lp == r for r in lowered_roots):
            continue
        if p not in alive:
            removed.add(p)
    if removed:
        c2 = connect()
        try:
            # 记录「首次被发现缺失」的时间供宽限期清理判断；已有时间戳保留，
            # 这样宽限期从第一次发现算起，而不是每次扫描都刷新。
            c2.executemany(
                "UPDATE movie_files SET missing = 1, missing_since = "
                "CASE WHEN COALESCE(missing,0) = 0 OR COALESCE(missing_since,'') = '' "
                "THEN datetime('now','localtime') ELSE missing_since END WHERE path = ?",
                [(p,) for p in removed])
            c2.commit()
        finally:
            c2.close()

    SCAN.update(phase="scanning", total=len(to_process), done=0, pending=len(to_process))

    # 3) 阶段一：并发解析 + 计算指纹 + 探测内嵌字幕（无 DB 写）
    #    字幕探测会起 ffprobe/ffmpeg，单独限并发，避免和解析线程一起把 NAS 打满
    sub_sem = threading.Semaphore(3)

    def worker(item):
        path, size, mtime, rec = item
        parsed = parse_file(path)
        qh = _compute_quick_hash(path, size, sample) if hash_files else 0
        strm = read_strm_target(path) if path.lower().endswith(".strm") else ""

        sub_tracks, sub_at = "", ""
        # ISO 是光盘镜像，ffprobe 读不到内部字幕轨（且容易超时），直接跳过
        if probe_subs and not strm and not path.lower().endswith(".iso"):
            unchanged = bool(
                rec and not rec["missing"]
                and abs(float(rec["size"] or 0) - size) < 0.5
                and abs(float(rec["mtime"] or 0) - mtime) < 1
            )
            if unchanged and (rec["sub_probe_at"] or ""):
                # 文件没变：直接复用上次探测结果，零开销
                sub_tracks = rec["sub_tracks"] or ""
                sub_at = rec["sub_probe_at"] or ""
            else:
                with sub_sem:
                    info = subprobe.probe(path, cfg)
                if info.get("probed_at"):
                    sub_at = info["probed_at"]
                    sub_tracks = json.dumps(info, ensure_ascii=False)

        return {"parsed": parsed, "path": path, "size": size, "mtime": mtime,
                "qh": qh, "strm": strm, "sub_tracks": sub_tracks, "sub_at": sub_at}

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
                act = store_upsert(conn, r["parsed"], r["path"], r["size"], r["mtime"], r["qh"],
                                   strm_url=r.get("strm") or "",
                                   sub_tracks=r.get("sub_tracks") or "",
                                   sub_probe_at=r.get("sub_at") or "")
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

    # 4.5) 本地元数据导入：把同目录的 .nfo/.json 读进来（离线，含番号回写），
    #      让「新加入的库」扫描完即带上本地整理好的信息，无需再手动刮削。
    SCAN.update(phase="local_nfo", current="正在导入本地 NFO/JSON…")
    local_nfo_hit = import_local_nfo_after_scan(progress_cb)

    # 4.6) 外挂字幕登记：同目录的 .srt/.ass/... 直接入库，
    #      AI 剧情/问答、播放器字幕菜单都不再需要手动上传对齐。
    SCAN.update(phase="subtitles", current="正在登记外挂字幕…")
    sidecar_subs = register_sidecar_subtitles_after_scan(progress_cb)

    # 5) 封面嗅探（单点执行，避免多线程竞争同一 movie 的封面）
    batch_local_covers(progress_cb)

    # 6) 自动清理：删除磁盘已消失的文件记录、空影片、孤儿元数据，保持库与磁盘一致
    if auto_cleanup is None:
        auto_cleanup = bool(cfg.get("library", {}).get("auto_cleanup", True))
    cleaned = {"files": 0, "movies": 0, "orphans": {}}
    if auto_cleanup:
        SCAN.update(phase="cleaning", current="正在清理失效记录…")
        cleaned = auto_cleanup_after_scan(alive, roots, progress_cb)

    SCAN.finish(
        "扫描完成：新增 %d / 更新 %d / 跳过 %d / 本地元数据 %d / 外挂字幕 %d / 缺失 %d / 清理 %d"
        % (added, updated, skipped, local_nfo_hit, sidecar_subs, len(removed),
           cleaned["files"] + cleaned["movies"] + sum(cleaned["orphans"].values()))
    )
    return {"total": enumerated, "added": added, "updated": updated,
            "unchanged": unchanged, "missing": len(removed), "errors": errors,
            "local_nfo": local_nfo_hit, "sidecar_subs": sidecar_subs, "cleaned": cleaned}


def auto_cleanup_after_scan(alive_paths: set, roots: list, progress_cb=None):
    """扫描后自动清理：

    1. 删除位于扫描根目录下、本次未出现在磁盘上的文件记录（prune_missing）；
    2. 连带删除因此失去所有文件的影片；
    3. 清理孤儿元数据（女优 / 类型 / 厂商 / 系列 / 标签）。
    全程基于本次扫描的 alive_paths 与 roots，不会误删其它目录的记录。
    """
    from . import store
    if progress_cb is None:
        progress_cb = SCAN.update
    # 宽限期：文件消失后先保留记录，超过设定天数才真正清理（0=发现即清理）
    _cfg = load_config()
    grace_days = max(0, int(_cfg.get("library", {}).get("missing_grace_days", 0) or 0))
    conn = connect()
    try:
        removed_files = store.prune_missing(conn, alive_paths, roots, grace_days=grace_days)
        orphans = store.cleanup_orphans(conn)
        conn.commit()
    finally:
        conn.close()
    cleaned = {"files": removed_files, "movies": 0, "orphans": orphans}
    # 统计被清理的空影片数（prune_missing 会连带删除无文件的影片，这里近似记为 0，
    # 真正删除数体现在 orphans 之外；如需精确可再查，但意义不大，故省略）
    SCAN.bump("cleaned_files", removed_files)
    for k, v in orphans.items():
        SCAN.bump(f"cleaned_{k}", v)
    progress_cb(cleaned_files=removed_files, cleaned_orphans=orphans)
    return cleaned


def store_upsert(conn, parsed, path, size, mtime, qh, strm_url="",
                 sub_tracks="", sub_probe_at=""):
    # 延迟导入，避免循环依赖
    from . import store
    return store.upsert_scanned_file(conn, parsed, path, size, mtime,
                                     quick_hash=qh, strm_url=strm_url,
                                     sub_tracks=sub_tracks, sub_probe_at=sub_probe_at)


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


def import_local_nfo_after_scan(progress_cb=None):
    """扫描后自动导入同目录的本地 NFO / JSON 元数据。

    只处理「还没有番号」或「还没刮削过」的影片：这两类最需要本地元数据来补全
    （已识别且已刮削的影片，其本地 NFO 更新由后续刮削按 local_nfo 优先级处理，
    扫描阶段不重复读盘）。命中后不标记 scraped_at，保留在线源补齐缺失字段的机会。
    返回成功导入的影片数。
    """
    from . import scraper
    if progress_cb is None:
        progress_cb = SCAN.update
    cfg = load_config()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT m.id FROM movies m "
            "WHERE EXISTS (SELECT 1 FROM movie_files f WHERE f.movie_id = m.id AND f.missing = 0) "
            "AND (m.has_code = 0 OR m.scraped_at = '')"
        ).fetchall()
        ids = [r["id"] for r in rows]
    finally:
        conn.close()
    total = len(ids)
    if not total:
        return 0
    hit = 0
    conn = connect()
    try:
        for i, mid in enumerate(ids):
            try:
                if scraper.import_local_nfo_one(conn, mid, cfg):
                    hit += 1
            except Exception:
                pass
            if (i + 1) % 20 == 0 or i == total - 1:
                progress_cb(local_nfo_done=i + 1, local_nfo_total=total)
        conn.commit()
    finally:
        conn.close()
    return hit


def register_sidecar_subtitles_after_scan(progress_cb=None) -> int:
    """扫描后登记同目录的外挂字幕（.srt/.ass/...）。

    只做「登记」，不动磁盘：用户把字幕放到视频旁边，扫描一次就能在详情页看到、
    播放器能选中、AI 剧情/问答/翻译也有输入。重复扫描是幂等的。
    返回新增登记条数。
    """
    from . import subtitles
    if progress_cb is None:
        progress_cb = SCAN.update
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT f.movie_id, f.path, m.code FROM movie_files f "
            "JOIN movies m ON m.id = f.movie_id "
            "WHERE COALESCE(f.missing, 0) = 0 AND COALESCE(f.strm_url, '') = ''"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return 0

    by_movie: dict = {}
    for r in rows:
        item = by_movie.setdefault(r["movie_id"], {"code": r["code"] or "", "paths": []})
        item["paths"].append(r["path"])

    total = len(by_movie)
    added = 0
    conn = connect()
    try:
        for i, (mid, info) in enumerate(by_movie.items()):
            try:
                added += subtitles.register_sidecars(conn, mid, info["paths"], info["code"])
            except Exception:
                pass
            if (i + 1) % 20 == 0 or i == total - 1:
                progress_cb(sidecar_done=i + 1, sidecar_total=total)
        conn.commit()
    finally:
        conn.close()
    return added


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
