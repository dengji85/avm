# -*- coding: utf-8 -*-
"""竖版海报批量生成任务：后台线程 + 进度轮询 + 可取消。

为什么单独成模块：
  * 批量联网（可选 DMM 官方竖版）在大库上是分钟级操作，同步 HTTP 会把
    浏览器/反代拖到超时，必须后台跑；
  * 进度语义（总数/已完成/失败/当前片）只服务这一个场景，塞进 api.py
    会让本来就大的路由文件更难读 —— 后端每加一个「有状态的长时间任务」，
    约定都放进自己的小模块，路由层只留三行薄壳。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from . import images
from .config import load_config
from .db import db
from .threadpool import submit_long

# 单任务状态。字段与前端轮询直接对齐：running / total / done / percent / message…
_JOB: Dict[str, Any] = {
    "running": False,
    "total": 0,
    "done": 0,
    "generated": 0,
    "skipped": 0,
    "failed": 0,
    "upgraded": 0,
    "percent": 0,
    "message": "",
    "cancel": False,
    "started_at": 0,
    "finished_at": 0,
}

_LOCK = threading.Lock()


def state() -> Dict[str, Any]:
    with _LOCK:
        return dict(_JOB)


def _set(**fields: Any) -> None:
    with _LOCK:
        _JOB.update(fields)


def start(force: bool = False, upgrade: bool = False) -> Dict[str, Any]:
    """启动批量生成；已在跑时直接返回当前进度（幂等，防重复点击）。"""
    with _LOCK:
        if _JOB.get("running"):
            return dict(_JOB)
        _JOB.update({
            "running": True, "total": 0, "done": 0, "generated": 0, "skipped": 0,
            "failed": 0, "upgraded": 0, "percent": 0,
            "message": "准备中…", "cancel": False,
            "started_at": int(time.time()), "finished_at": 0,
        })
    submit_long(_run, bool(force), bool(upgrade))
    return state()


def cancel() -> Dict[str, Any]:
    _set(cancel=True, message="正在取消…")
    return state()


def _rows() -> List[Dict[str, Any]]:
    with db() as conn:
        cur = conn.execute(
            "SELECT id, code, key, cover FROM movies WHERE cover <> '' ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


def _run(force: bool, upgrade: bool) -> None:
    """任务主体：逐部生成竖版海报（可选 DMM 高清升级）。异常不外抛，写进 message。"""
    try:
        cfg = load_config(refresh=True)
        rows = _rows()
        total = len(rows)
        _set(total=total, message="已开始…" if total else "没有需要处理的影片")
        generated = skipped = failed = upgraded = 0
        for idx, r in enumerate(rows, 1):
            if state().get("cancel"):
                _set(message="已取消", percent=int((idx - 1) * 100 / total) if total else 100)
                break
            cover = str(r.get("cover") or "")
            code = str(r.get("code") or "").strip()
            name = ""
            try:
                if not cover:
                    skipped += 1
                elif not force and not upgrade and images.poster_path(cover) is not None:
                    skipped += 1
                else:
                    if force:
                        old = images.poster_path(cover)
                        if old is not None:
                            try:
                                old.unlink()
                            except OSError:
                                pass
                    if upgrade and code:
                        up = images.upgrade_from_dmm(str(r.get("key") or ""), code, cfg)
                        if up.get("cover"):
                            cover = up["cover"]
                            upgraded += 1
                            with db() as conn:
                                conn.execute("UPDATE movies SET cover = ? WHERE id = ?",
                                             (cover, r["id"]))
                                conn.commit()
                    name = images.ensure_poster(cover, cfg, code=code,
                                                allow_network=upgrade)
                    if name:
                        generated += 1
                    else:
                        failed += 1
            except Exception:                       # 单部失败不影响整批
                failed += 1
            _set(done=idx, generated=generated, skipped=skipped,
                 failed=failed, upgraded=upgraded,
                 percent=int(idx * 100 / total) if total else 100,
                 message="处理中：%s" % (code or str(r.get("id"))))
        _set(running=False, finished_at=int(time.time()),
             message="完成：生成 %d，跳过 %d，失败 %d，封面升级 %d"
                     % (generated, skipped, failed, upgraded))
    except Exception as e:                       # noqa: BLE001 兜底：绝不让任务线程静默死掉
        _set(running=False, finished_at=int(time.time()),
             message="任务异常：%s" % str(e)[:160])
