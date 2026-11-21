# -*- coding: utf-8 -*-
"""订阅（女优 / 系列）新片提醒：目标、发现、规则与扫描。

路由只做参数转发与错误翻译；扫描、判定、资源候选都在 ``app/subscription.py``，
批量任务在 ``app/subscription_job.py``。设计见 docs/订阅新片提醒设计.md。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse

from .. import subscription, subscription_job, subscription_probe_job, subscription_schedule
from ..config import load_config
from ..db import db, query_one

router = APIRouter()


def _fail(e: Exception) -> HTTPException:
    if isinstance(e, KeyError):
        return HTTPException(404, str(e.args[0]) if e.args else "记录不存在")
    return HTTPException(400, str(e))


# ------------------------------------------------------------------ 目标
@router.get("/subscriptions/targets")
def list_targets() -> Dict[str, Any]:
    with db() as conn:
        return {"items": subscription.list_targets(conn)}


@router.post("/subscriptions/targets")
def create_target(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    with db() as conn:
        try:
            return {"ok": True, "item": subscription.create_target(conn, payload or {})}
        except Exception as e:  # noqa: BLE001 统一翻成 400/404
            raise _fail(e)


@router.get("/subscriptions/library-options")
def library_options(kind: str = "actress", q: str = "", limit: int = 60,
                    followed_only: bool = False,
                    subscribed_only: bool = False) -> Dict[str, Any]:
    """库内可订阅的女优 / 系列（不再要求用户手填站点 ID）。"""
    with db() as conn:
        try:
            return {"items": subscription.library_options(
                conn, kind=kind, q=q, limit=limit, followed_only=followed_only,
                subscribed_only=subscribed_only)}
        except Exception as e:  # noqa: BLE001
            raise _fail(e)


@router.post("/subscriptions/targets/from-library")
def create_target_from_library(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    with db() as conn:
        try:
            return {"ok": True, "item": subscription.create_target_from_library(
                conn, load_config(), payload or {})}
        except Exception as e:  # noqa: BLE001
            raise _fail(e)


@router.post("/subscriptions/targets/from-library/bulk")
def create_targets_from_library(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """一键订阅库里所有已关注女优（已订阅的跳过）。"""
    with db() as conn:
        try:
            return {"ok": True, **subscription.create_targets_from_library(
                conn, load_config(), payload or {})}
        except Exception as e:  # noqa: BLE001
            raise _fail(e)


@router.patch("/subscriptions/targets/{target_id}")
def update_target(target_id: int, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    with db() as conn:
        try:
            return {"ok": True, "item": subscription.update_target(conn, target_id, payload or {})}
        except Exception as e:  # noqa: BLE001
            raise _fail(e)


@router.delete("/subscriptions/targets/{target_id}")
def delete_target(target_id: int) -> Dict[str, Any]:
    with db() as conn:
        subscription.delete_target(conn, target_id)
        return {"ok": True}


# ------------------------------------------------------------------ 发现
@router.get("/subscriptions/findings")
def list_findings(status: str = "", target_id: Optional[int] = Query(default=None),
                  site: str = "", date_from: str = "", date_to: str = "",
                  q: str = "", actress: str = "", sort: str = "release",
                  cursor: str = "", limit: int = 50) -> Dict[str, Any]:
    with db() as conn:
        return subscription.list_findings(
            conn, status=status, target_id=target_id, site=site,
            date_from=date_from, date_to=date_to, q=q, actress=actress, sort=sort,
            cursor=cursor, limit=limit, cfg=load_config())


@router.get("/subscriptions/findings/actresses")
def finding_actresses(target_id: Optional[int] = Query(default=None),
                      limit: int = 200) -> Dict[str, Any]:
    """发现里的女优聚合，供筛选下拉。"""
    with db() as conn:
        return {"items": subscription.list_finding_actresses(
            conn, target_id=target_id, limit=limit)}


@router.get("/subscriptions/findings/{finding_id}/cover")
def finding_cover(finding_id: int, size: str = "") -> Any:
    """发现封面：后端取回并缓存，避免浏览器直连外站被墙 / 防盗链。

    size=large 用于「放大看封面」：优先官方大图，拿不到自动退回列表缩略图。
    """
    with db() as conn:
        try:
            path = subscription.ensure_finding_cover(conn, finding_id,
                                                     large=size == "large")
        except Exception as e:  # noqa: BLE001
            raise _fail(e)
    if not path or not path.exists():
        raise HTTPException(404, "封面暂不可用")
    return FileResponse(str(path), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.post("/subscriptions/findings/{finding_id}/read")
def read_finding(finding_id: int) -> Dict[str, Any]:
    with db() as conn:
        try:
            return subscription.mark_read(conn, finding_id)
        except Exception as e:  # noqa: BLE001
            raise _fail(e)


@router.post("/subscriptions/findings/{finding_id}/unread")
def unread_finding(finding_id: int) -> Dict[str, Any]:
    with db() as conn:
        try:
            return subscription.mark_unread(conn, finding_id)
        except Exception as e:  # noqa: BLE001
            raise _fail(e)


@router.post("/subscriptions/seen")
def mark_seen() -> Dict[str, Any]:
    """进入新片列表时调用：顶部未读徽章清零（不影响单条的已读/未读状态）。"""
    with db() as conn:
        return {"ok": True, "seen_at": subscription.mark_seen(conn)}


@router.post("/subscriptions/findings/{finding_id}/ignore")
def ignore_finding(finding_id: int) -> Dict[str, Any]:
    with db() as conn:
        try:
            return subscription.ignore(conn, finding_id)
        except Exception as e:  # noqa: BLE001
            raise _fail(e)


@router.post("/subscriptions/findings/{finding_id}/restore")
def restore_finding(finding_id: int) -> Dict[str, Any]:
    with db() as conn:
        try:
            return subscription.restore(conn, finding_id)
        except Exception as e:  # noqa: BLE001
            raise _fail(e)


@router.post("/subscriptions/findings/{finding_id}/save")
def save_finding(finding_id: int) -> Dict[str, Any]:
    with db() as conn:
        try:
            return subscription.save_finding(conn, finding_id)
        except Exception as e:  # noqa: BLE001
            raise _fail(e)


@router.post("/subscriptions/findings/bulk")
def bulk_findings(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """批量改状态：已读 / 未读 / 忽略 / 恢复 / 归档。"""
    data = payload or {}
    with db() as conn:
        try:
            return subscription.bulk_findings(conn, data.get("ids") or [], data.get("action") or "")
        except Exception as e:  # noqa: BLE001
            raise _fail(e)


@router.get("/subscriptions/findings/{finding_id}/resources")
def finding_resources(finding_id: int) -> Dict[str, Any]:
    with db() as conn:
        try:
            return subscription.resources(conn, finding_id)
        except Exception as e:  # noqa: BLE001
            raise _fail(e)


@router.post("/subscriptions/findings/{finding_id}/probe")
def probe_one_finding(finding_id: int) -> Dict[str, Any]:
    """单条立即重探（卡片上的「重新探测」按钮）。

    走同步调用：只有一条、站点探测本身是秒级 IO；不排后台任务，免得跟批量探测抢外站。
    """
    with db() as conn:
        try:
            result = subscription.probe_finding(conn, finding_id, cfg=load_config(),
                                               delay=0.0, ttl_days=0)
        except Exception as e:  # noqa: BLE001
            raise _fail(e)
        row = query_one(conn, "SELECT probe_site, probe_status, probe_kind, probe_at "
                              "FROM subscription_findings WHERE id = ?", (finding_id,)) or {}
    return {"ok": True, "result": result,
            "probe_site": row.get("probe_site") or "",
            "probe_status": row.get("probe_status") or "",
            "probe_kind": row.get("probe_kind") or "",
            "probe_at": row.get("probe_at") or ""}


# ------------------------------------------------------------------ 规则
@router.get("/subscriptions/rules")
def list_rules(target_id: Optional[int] = Query(default=None)) -> Dict[str, Any]:
    with db() as conn:
        return {"items": subscription.list_rules(conn, target_id),
                "rule_types": subscription.RULE_TYPES}


@router.post("/subscriptions/rules")
def create_rule(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    with db() as conn:
        try:
            return {"ok": True, "item": subscription.create_rule(conn, payload or {})}
        except Exception as e:  # noqa: BLE001
            raise _fail(e)


@router.patch("/subscriptions/rules/{rule_id}")
def update_rule(rule_id: int, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    with db() as conn:
        try:
            return {"ok": True, "item": subscription.update_rule(conn, rule_id, payload or {})}
        except Exception as e:  # noqa: BLE001
            raise _fail(e)


@router.delete("/subscriptions/rules/{rule_id}")
def delete_rule(rule_id: int) -> Dict[str, Any]:
    with db() as conn:
        subscription.delete_rule(conn, rule_id)
        return {"ok": True}


@router.post("/subscriptions/rules/preview")
def preview_rules(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    with db() as conn:
        try:
            return subscription.preview_rules(conn, payload or {})
        except Exception as e:  # noqa: BLE001
            raise _fail(e)


# ------------------------------------------------------------------ 扫描 / 探测 / 未读
@router.post("/subscriptions/scan")
def start_scan(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """启动扫描。

    ``target_ids`` 非空时只扫这些目标（新增订阅后单查）；
    ``mode="backfill"`` 走历史补抓（翻页更久、不提前停）。
    """
    data = payload or {}
    ids = data.get("target_ids") or []
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        raise HTTPException(400, "target_ids 必须是订阅目标 id 数组")
    mode = str(data.get("mode") or "").strip().lower()
    if mode not in ("", "backfill"):
        raise HTTPException(400, "mode 只支持 backfill")
    return subscription_job.start(target_ids=ids, mode=mode)


@router.get("/subscriptions/scan/state")
def scan_state() -> Dict[str, Any]:
    return subscription_job.state()


@router.get("/subscriptions/schedule")
def schedule_state() -> Dict[str, Any]:
    """自动扫描的定时状态：模式、上次 / 下次时间、表达式是否合法。"""
    return subscription_schedule.state()


@router.get("/subscriptions/schedule/preview")
def schedule_preview(expr: str = "", count: int = 5) -> Dict[str, Any]:
    """校验 cron 表达式并列出接下来几次触发时间（设置界面用）。"""
    return subscription_schedule.preview(expr, count)


@router.post("/subscriptions/scan/cancel")
def cancel_scan() -> Dict[str, Any]:
    return subscription_job.cancel()


@router.post("/subscriptions/probe")
def start_probe(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """探测发现里的作品在在线站点上有没有资源页（后台任务，可取消）。"""
    return subscription_probe_job.start(force=bool((payload or {}).get("force")))


@router.get("/subscriptions/probe/state")
def probe_state() -> Dict[str, Any]:
    return subscription_probe_job.state()


@router.post("/subscriptions/probe/cancel")
def cancel_probe() -> Dict[str, Any]:
    return subscription_probe_job.cancel()


@router.get("/subscriptions/unread-count")
def unread_count() -> Dict[str, Any]:
    with db() as conn:
        return {"count": subscription.unread_count(conn),
                "cooldowns": subscription.cooldowns()}
