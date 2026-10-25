# -*- coding: utf-8 -*-
"""把刮削结果落盘到影片目录（NFO + 封面 + fanart），供 Emby / Jellyfin / Kodi 识别。

设计约束：
- 本模块运行在刮削热路径上，**不向外抛异常**：写盘失败（只读挂载、网络盘掉线、
  权限不足）绝不应拖垮刮削任务本身，失败统一以结果字典表达。
- 默认保守：已有 NFO 一律不动（skip）；要覆盖必须先备份 .nfo.bak。
- 图片默认命名为 ``<视频名>-poster.jpg``，同目录多部影片不会互相覆盖。
- ``<art>`` 里写**相对文件名**，不写绝对路径/远程 URL（换机器、换挂载点会失效）。
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import emby, images, store
from . import nfo as nfo_mod
from .config import COVER_DIR, fanart_dir, load_config, nfo_export_cfg
from .db import connect, query_all

# 媒体服务器普遍支持、无需转换的图片扩展名
_IMG_OK = {".jpg", ".jpeg", ".png"}


# ------------------------------------------------------------------ 路径与命名


def _map_path(path: str, ec: Dict[str, Any]) -> Path:
    """按配置的 path_map 做前缀替换（宿主机 ↔ 容器/网盘路径不一致时用）。"""
    raw = str(path or "")
    for item in ec.get("path_map") or []:
        try:
            frm = str(item.get("from") or "").rstrip("/\\")
            to = str(item.get("to") or "").rstrip("/\\")
        except AttributeError:
            continue
        if not frm or not to:
            continue
        if raw.lower().startswith(frm.lower()):
            return Path(to + raw[len(frm):])
    return Path(raw)


def _poster_base(folder: Path, video: Path, ec: Dict[str, Any], kind: str = "poster") -> Path:
    """返回**不含扩展名**的目标主名。

    numbered（默认）= ``<视频名>-poster``；plain = ``poster``。
    多分片（-cd1/-cd2）同目录时，numbered 也能各自独立，不互相覆盖。
    """
    naming = str(ec.get("image_naming") or "numbered").lower()
    if naming == "plain":
        return folder / kind
    return folder / ("%s-%s" % (video.stem, kind))


def _nfo_target(folder: Path, video: Path, ec: Dict[str, Any]) -> Path:
    naming = str(ec.get("nfo_naming") or "match-video").lower()
    if naming == "movie":
        return folder / "movie.nfo"
    return folder / (video.stem.strip() + ".nfo")


def _writable(folder: Path) -> bool:
    """试写一个临时文件判断目录可写（比 os.access 可靠，能覆盖网络盘/只读挂载）。"""
    probe = folder / ".avm_write_test"
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("x")
        return True
    except Exception:
        return False
    finally:
        try:
            probe.unlink()
        except Exception:
            pass


# ------------------------------------------------------------------ 图片落盘


def _copy_image(src: Path, base: Path, force: bool = False) -> Optional[Path]:
    """把图片复制为 ``base + 扩展名``。返回实际写出的路径，失败返回 None。"""
    try:
        if not src or not src.is_file():
            return None
    except OSError:
        return None

    ext = src.suffix.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    if ext not in _IMG_OK:
        # webp/gif/bmp：媒体服务器支持不一致，能转就转成 jpg
        try:
            from PIL import Image
            dst = base.with_name(base.name + ".jpg")
            if not (dst.exists() and not force and _same_size(dst, src)):
                Image.open(src).convert("RGB").save(dst, "JPEG", quality=92)
            return dst
        except Exception:
            ext = src.suffix.lower() or ".jpg"

    dst = base.with_name(base.name + ext)
    try:
        if dst.exists() and not force and _same_size(dst, src):
            return dst
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        return dst
    except Exception:
        return None


def _same_size(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size
    except OSError:
        return False


# ------------------------------------------------------------------ NFO 落盘


def _backup(target: Path) -> None:
    try:
        shutil.copyfile(target, target.with_name(target.name + ".bak"))
    except Exception:
        pass


def _write_nfo(target: Path, movie: Dict[str, Any], poster: str, fanart: str,
               ec: Dict[str, Any]) -> str:
    """写 NFO，返回状态：written / exists_skip / failed。"""
    strategy = str(ec.get("on_conflict") or "skip").lower()
    existed = target.exists()

    # 同目录已有「另一种命名」的 NFO（movie.nfo / 番号.nfo）时也算已存在，
    # 避免同一个视频出现两份 NFO；merge/overwrite 都写回已有那一份。
    if not existed:
        side = nfo_mod.find_sidecar(movie_file_of(movie), str(movie.get("code") or ""))
        if side is not None:
            if strategy == "skip":
                return "exists_skip"
            target = side
            existed = True

    if existed and strategy == "skip":
        return "exists_skip"

    existing_root = None
    if existed and strategy == "merge":
        existing_root = nfo_mod.read_nfo_root(target)
        if existing_root is None:  # 已有文件损坏 → 走「备份 + 重写」
            _backup(target)
    elif existed:
        if ec.get("backup", True):
            _backup(target)

    fresh = nfo_mod.build_nfo_root(movie, poster=poster, fanart=fanart,
                                   rating_field=str(ec.get("rating_field") or "userrating"))
    root = nfo_mod.merge_into(existing_root, fresh) if existing_root is not None else fresh

    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(nfo_mod.pretty(root), encoding="utf-8")
        os.replace(tmp, target)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        return "failed"

    # 落盘后读回校验：自己写的东西必须能被自己解析，否则宁可报失败
    if nfo_mod.read_nfo_root(target) is None:
        return "failed"
    return "written"


def movie_file_of(movie: Dict[str, Any]) -> str:
    files = movie.get("files") or []
    return str(files[0].get("path") or "") if files else ""


# ------------------------------------------------------------------ 单部导出


def export_detail(movie: Dict[str, Any], cfg: Dict[str, Any] | None = None,
                  force: bool = False) -> Dict[str, Any]:
    """把一部影片写到磁盘。永不抛异常。

    返回 {ok, nfo, poster, fanart, nfo_status, skipped, error, dirs}
    - ok        ：是否有任意产物写成功
    - nfo_status：written / exists_skip / failed / ""（未开启写 NFO）
    - skipped   ：什么都没写成时的原因：disabled / no_file / no_dir / read_only /
                  exists_skip / no_cover
    """
    cfg = cfg or load_config()
    ec = nfo_export_cfg(cfg)
    res: Dict[str, Any] = {"ok": False, "nfo": "", "poster": "", "fanart": "",
                           "nfo_status": "", "skipped": "", "error": "", "dirs": []}
    if not ec.get("enabled") and not force:
        res["skipped"] = "disabled"
        return res
    if not movie:
        res["skipped"] = "no_file"
        return res

    # 只写「文件确实在磁盘上」的条目：缺失文件所在目录可能已不存在
    files = [f for f in (movie.get("files") or [])
             if f.get("path") and not f.get("missing")]
    if not files:
        res["skipped"] = "no_file"
        return res

    # 同一目录的多个分片只写一份 sidecar
    groups: Dict[str, Tuple[Path, Path]] = {}
    for f in files:
        video = _map_path(str(f["path"]), ec)
        folder = video.parent
        groups.setdefault(str(folder), (folder, video))

    poster_src = None
    if ec.get("write_cover", True):
        cover = str(movie.get("cover") or "")
        if cover and not cover.lower().startswith(("http://", "https://")):
            poster_src = images.cover_path(cover)
        if poster_src is None:
            poster_src = images.find_local_cover(movie_file_of(movie), str(movie.get("code") or ""))

    fanart_src = None
    if ec.get("write_fanart", True) and str(movie.get("fanart") or ""):
        cand = fanart_dir(cfg) / str(movie["fanart"])
        if cand.is_file():
            fanart_src = cand

    nfo_written = 0
    nfo_skipped = False
    for folder, video in groups.values():
        if not folder.is_dir():
            res["skipped"] = res["skipped"] or "no_dir"
            continue
        if not _writable(folder):
            res["skipped"] = "read_only"
            continue
        res["dirs"].append(str(folder))

        poster_name = ""
        if poster_src is not None:
            dst = _copy_image(poster_src, _poster_base(folder, video, ec, "poster"), force)
            if dst is not None:
                poster_name = dst.name
                res["poster"] = str(dst)

        fanart_name = ""
        if fanart_src is not None:
            dst = _copy_image(fanart_src, _poster_base(folder, video, ec, "fanart"), force)
            if dst is not None:
                fanart_name = dst.name
                res["fanart"] = str(dst)

        if ec.get("write_nfo", True):
            target = _nfo_target(folder, video, ec)
            try:
                status = _write_nfo(target, movie, poster_name, fanart_name, ec)
            except Exception as exc:  # 兜底：任何意外都只记录，不冒泡
                status = "failed"
                res["error"] = str(exc)[:200]
            if status == "written":
                nfo_written += 1
                res["nfo"] = str(target)
                res["nfo_status"] = "written"
            elif status == "exists_skip":
                nfo_skipped = True
                res["nfo_status"] = res["nfo_status"] or "exists_skip"
            else:
                res["nfo_status"] = "failed"
                res["error"] = res["error"] or "NFO 写入失败"

    res["ok"] = bool(nfo_written or res["poster"] or res["fanart"])
    if not res["ok"] and not res["error"]:
        # 什么都没写成：给一个可读原因（已有 NFO 跳过 / 无封面可写）
        res["skipped"] = res["skipped"] or ("exists_skip" if nfo_skipped else "no_cover")
    if res["ok"] and ec.get("notify_emby", True):
        try:
            emby.notify_soon()
        except Exception:
            pass
    return res


def export_movie(conn, movie_id: int, cfg: Dict[str, Any] | None = None,
                 force: bool = False) -> Dict[str, Any]:
    """按 id 导出（自己取详情）。永不抛异常。"""
    try:
        movie = store.movie_detail(conn, int(movie_id))
    except Exception as exc:
        return {"ok": False, "skipped": "", "error": str(exc)[:200]}
    return export_detail(movie, cfg=cfg, force=force)


# ------------------------------------------------------------------ 批量导出


def select_targets(conn, ids: Optional[List[int]] = None,
                   only_scraped: bool = True) -> List[int]:
    """批量导出的候选影片 id（默认只挑已刮削过的）。"""
    if ids:
        return [int(i) for i in ids]
    sql = "SELECT id FROM movies WHERE 1=1"
    if only_scraped:
        sql += " AND COALESCE(scraped_at, '') <> ''"
    sql += " ORDER BY id"
    rows = query_all(conn, sql)
    return [int(r["id"]) for r in rows]


def run_export(ids: List[int], cfg: Dict[str, Any] | None = None, force: bool = False,
               job=None) -> Dict[str, Any]:
    """批量导出主循环（在后台线程里跑）。永不抛异常。"""
    cfg = cfg or load_config()
    summary = {"total": len(ids), "written": 0, "skipped": 0, "failed": 0}
    if job is not None:
        # 调用方通常已经 job.start()（用于原子抢占），这里只在未启动时兜底
        if not job.running:
            job.start()
        job.update(total=len(ids), phase="running")
    conn = connect()
    try:
        for idx, mid in enumerate(ids, 1):
            if job is not None and job.cancelled:
                break
            res = export_movie(conn, mid, cfg=cfg, force=force)
            if res.get("ok"):
                summary["written"] += 1
            elif res.get("error"):
                summary["failed"] += 1
                if job is not None:
                    job.error("#%s %s" % (mid, res.get("error")))
            else:
                summary["skipped"] += 1
            if job is not None:
                job.update(total=len(ids), done=idx, current="#%s" % mid,
                           written=summary["written"], skipped=summary["skipped"],
                           failed=summary["failed"])
            time.sleep(0)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if job is not None:
        if job.cancelled:
            # 用户中途点了停止：如实说「已停止」，别让人以为全部导完了
            job.finish("已停止：成功 %d，跳过 %d，失败 %d" % (
                summary["written"], summary["skipped"], summary["failed"]))
        else:
            job.finish("导出完成：成功 %d，跳过 %d，失败 %d" % (
                summary["written"], summary["skipped"], summary["failed"]))
    return summary
