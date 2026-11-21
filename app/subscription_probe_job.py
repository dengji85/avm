# -*- coding: utf-8 -*-
"""订阅发现「在线资源探测」批量任务：后台线程 + 进度轮询 + 可取消。

为什么单独成模块：一次探测要逐条访问 3～6 个外站，几十条发现就是分钟级任务；
放同步请求里会被浏览器 / 反代 60s 判超时。形态照 poster_job.py / translate_job.py。

判据见 subscription.probe_finding：只认「200 且页面里出现番号」，网络失败保持未知，
下次再探测仍会重试，不会把被墙误记成「无资源」。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from . import subscription
from .config import load_config
from .db import db, query_all
from .threadpool import submit_long

_JOB: Dict[str, Any] = {
    "running": False,
    "total": 0,
    "done": 0,
    "found": 0,
    "missing": 0,
    "unknown": 0,
    "skipped": 0,
    "percent": 0,
    "message": "",
    "cancel": False,
    "finished_at": 0,
}

_LOCK = threading.Lock()
#: 单次最多探测多少条：外站请求量要可控，剩下的再点一次即可
_MAX_ITEMS = 200
#: 相邻站点 / 相邻发现之间的间隔（秒），别把站点打烦
_DELAY = 1.2


def state() -> Dict[str, Any]:
    with _LOCK:
        return dict(_JOB)


def _set(**fields: Any) -> None:
    with _LOCK:
        _JOB.update(fields)


def _cancelled() -> bool:
    with _LOCK:
        return bool(_JOB.get("cancel"))


def start(force: bool = False, limit: int = 0) -> Dict[str, Any]:
    """启动探测。重复点击返回同一任务，不排第二次。"""
    with _LOCK:
        if _JOB.get("running"):
            return dict(_JOB)
        _JOB.update({"running": True, "total": 0, "done": 0, "found": 0, "missing": 0,
                     "unknown": 0, "skipped": 0, "percent": 0, "message": "准备中…",
                     "cancel": False, "finished_at": 0})
    submit_long(_run, bool(force), int(limit or _MAX_ITEMS))
    return state()


#: 延迟自动探测：同一时间只排一个，避免扫描频繁时叠加请求
_AUTO_TIMER: Optional[threading.Timer] = None
_AUTO_LOCK = threading.Lock()


def schedule_auto(delay: Optional[float] = None) -> bool:
    """扫描结束 / 服务启动后延迟跑一轮（只补没探测过的条目）。

    延迟的意义：先让用户看到新片列表，也错开刚扫完站点的请求；探测跑在长任务池，
    不阻塞任何请求。已有待跑任务时不重复排。
    """
    cfg = load_config(refresh=True)
    sub = cfg.get("subscription") or {}
    if not sub.get("enabled", True) or not sub.get("auto_probe", True):
        return False
    wait = float(delay if delay is not None else (sub.get("auto_probe_delay_seconds") or 180))
    global _AUTO_TIMER
    with _AUTO_LOCK:
        if _AUTO_TIMER is not None and _AUTO_TIMER.is_alive():
            return False
        _AUTO_TIMER = threading.Timer(max(5.0, wait), _run_auto)
        _AUTO_TIMER.daemon = True
        _AUTO_TIMER.start()
    return True


def _run_auto() -> None:
    """定时器回调：按配置的批量上限跑一轮（人工任务在跑时 start 会直接返回）。"""
    try:
        sub = load_config(refresh=True).get("subscription") or {}
        limit = int(sub.get("auto_probe_batch") or 40)
    except Exception:
        limit = 40
    start(force=False, limit=limit)


def cancel() -> Dict[str, Any]:
    with _LOCK:
        if _JOB.get("running"):
            _JOB["cancel"] = True
    return state()


def _run(force: bool, limit: int) -> None:
    try:
        cfg = load_config(refresh=True)
        sites = subscription.probe_sites(cfg)
        if not sites:
            _set(message="没有可用于探测的站点（需要按番号直达作品页的在线站点）",
                 running=False, percent=100, finished_at=1)
            return
        sub = cfg.get("subscription") or {}
        # 结论 TTL：站点上架 / 下架是慢变化，过期了才重问（手动检查传 force 立即重问）
        ttl = 0 if force else max(1, int(sub.get("probe_ttl_days") or 7))
        base = ["f.status NOT IN ('ignored', 'archived')",
                # 已入库的作品不再提醒，也就没必要再花外站请求
                "NOT EXISTS (SELECT 1 FROM movies m WHERE m.code_norm = f.code_norm)"]
        conds = list(base)
        args: list = []
        skipped = 0
        if not force:
            # 没探测过的、元信息还空着的，以及结论已经过期的
            conds.append("(f.probe_status = '' OR f.meta_json IN ('', '{}') "
                         "OR (f.probe_at <> '' AND f.probe_at < ?))")
            args.append((datetime.now() - timedelta(days=ttl)).strftime("%Y-%m-%d %H:%M:%S"))
            # 只见过 1 次的先不动：多半是列表页抖动 / 重复渲染，等下次扫描确认后再说
            conds.append("f.confirm_count >= 2")
        sql = ("SELECT f.id FROM subscription_findings f WHERE " + " AND ".join(conds)
               + " ORDER BY f.release_date DESC, f.id DESC LIMIT ?")
        args.append(max(1, limit))
        with db() as conn:
            ids = [int(r["id"]) for r in query_all(conn, sql, args)]
            if not force:
                # 待确认（confirm_count < 2）的条数：只是先不检查，不是失败
                skip_rows = query_all(
                    conn,
                    "SELECT COUNT(*) AS c FROM subscription_findings f WHERE "
                    + " AND ".join(base) + " AND f.confirm_count < 2")
                skipped = int(skip_rows[0]["c"]) if skip_rows else 0
        total = len(ids)
        if not total:
            msg = "在线资源标记都是最新的，无需检查"
            if skipped:
                msg += "（另有 %d 条待确认，暂不检查）" % skipped
            _set(total=0, running=False, percent=100, finished_at=1,
                 skipped=skipped, message=msg)
            return
        _set(total=total, skipped=skipped, message="共 %d 条" % total)
        found = missing = unknown = 0
        for i, fid in enumerate(ids):
            if _cancelled():
                _set(message="已取消", running=False, finished_at=1)
                return
            try:
                with db() as conn:
                    res = subscription.probe_finding(conn, fid, cfg=cfg, sites=sites,
                                                     delay=_DELAY, ttl_days=ttl)
            except Exception:
                res = ""
            if res == "ok":
                found += 1
            elif res == "none":
                missing += 1
            else:
                unknown += 1
            done = i + 1
            _set(done=done, found=found, missing=missing, unknown=unknown,
                 percent=int(done * 100 / total) if total else 100,
                 message="%d / %d" % (done, total))
            if i < total - 1:
                time.sleep(_DELAY)
        msg = "完成：有资源 %d 条，无资源 %d 条，未知 %d 条" % (found, missing, unknown)
        if unknown:
            msg += "（未知＝站点不可达或被挡，下次会重试）"
        if skipped:
            msg += "；%d 条待确认未检查" % skipped
        _set(running=False, percent=100, message=msg, finished_at=1, skipped=skipped)
    except Exception as e:  # noqa: BLE001 任务失败不能让线程悬在 running
        _set(running=False, message="探测失败：%s" % e, finished_at=1)
