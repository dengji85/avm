# -*- coding: utf-8 -*-
"""浏览器接入端点（参数校验 + 转发，业务逻辑在 ``app/ingest.py`` /
``app/stream_verify.py``）。

统一挂在父级 ``/api`` 前缀下，鉴权沿用全局访问令牌 / 事务 API Key：
插件需带 ``X-Access-Token`` 或 ``Authorization: Bearer <API Key>``。
关闭「浏览器接入」只停用插件侧的 link / events 上报；本地已入库来源
仍可查看、验证、切换与删除。
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException, Query

router = APIRouter()


def _check(res: Dict[str, Any]) -> Dict[str, Any]:
    """业务错误统一成 400 + 中文可读原因；成功结果原样返回。"""
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message") or "操作失败")
    return res


@router.get("/ingest/capabilities")
def ingest_capabilities() -> Dict[str, Any]:
    """插件启动时做能力协商：契约版本、来源类型、事件类型、鉴权方式。"""
    from .. import ingest
    res = ingest.capabilities()
    res["verification"] = True
    res["statuses"] = ["valid", "stale", "invalid", "unsupported", "checking", "disabled"]
    return res


@router.get("/ingest/match")
def ingest_match(
    url: str = Query(default=""),
    code: str = Query(default=""),
    site: str = Query(default=""),
    site_id: str = Query(default=""),
    title: str = Query(default=""),
) -> Dict[str, Any]:
    """判断「库中已有 / 待加入 / 需人工确认」，供插件浮卡展示。"""
    from .. import ingest
    from ..db import db
    with db() as conn:
        return ingest.resolve(conn, {
            "page_url": url, "code": code, "site": site,
            "site_id": site_id, "title": title,
        })


@router.post("/ingest/link")
def ingest_link(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """幂等建片 / 追加来源：同一规范 URL 或站内 ID 不会挂到两部影片。"""
    from .. import ingest, stream_verify
    from ..db import db
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail="请求内容不能为空")
    with db() as conn:
        res = _check(ingest.link(conn, payload))
    # 事务提交后再排队轻验证：短任务用独立连接，不能抢在未提交的事务里读。
    # 只有这次真的拿到 / 换了播放地址才有必要验证；否则重复点「加入片库」
    # 会不停把来源重置成 checking，还会覆盖上次的验证结果。
    if (ingest.cfg().get("verify_on_link", True) and res.get("play_url_updated")
            and res.get("source_id")):
        stream_verify.submit_check(int(res["source_id"]))
    # 新片入库后自动刮削：事务已提交，刮削用独立连接；已有任务在跑时
    # 挂到待补队列（queue_movie_scrape 返回 busy），不会静默丢掉。
    # 追加来源（linked）不重复刮削，避免覆盖用户已修正的元数据。
    if res.get("status") == "created" and ingest.cfg().get("auto_scrape", True):
        from .. import scraper
        res["scrape"] = scraper.queue_movie_scrape(int(res["movie_id"]))
    return res


@router.post("/ingest/events")
def ingest_events(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """观看事件：start / heartbeat / pause / end → watch_sessions + watch_progress。"""
    from .. import ingest
    from ..db import db
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail="请求内容不能为空")
    with db() as conn:
        return _check(ingest.handle_event(conn, payload))


@router.get("/movies/{movie_id}/sources")
def movie_sources(movie_id: int) -> Dict[str, Any]:
    """详情页「来源」区数据。播放地址只回显脱敏后的域名部分。"""
    from .. import ingest
    from ..db import db, query_one
    with db() as conn:
        if not query_one(conn, "SELECT id FROM movies WHERE id = ?", (movie_id,)):
            raise HTTPException(status_code=404, detail="影片不存在")
        return {"items": ingest.list_sources(conn, movie_id)}


@router.post("/movies/{movie_id}/sources")
def movie_source_add(movie_id: int,
                     payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """手动登记来源：页面地址 / 播放地址 / 标题 / 类型。"""
    from .. import ingest, stream_verify
    from ..db import db
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail="请求内容不能为空")
    with db() as conn:
        res = _check(ingest.add_source(conn, movie_id, payload))
    if ingest.cfg().get("verify_on_link", True) and res.get("source_id"):
        stream_verify.submit_check(int(res["source_id"]))
    return res


@router.put("/sources/{source_id}/primary")
def source_primary(source_id: int) -> Dict[str, Any]:
    """把某个来源设为主播放源（记为「用户固定」，自动降级后可切回）。"""
    from .. import ingest
    from ..db import db
    with db() as conn:
        return _check(ingest.set_primary(conn, source_id))


@router.patch("/sources/{source_id}")
def source_toggle(source_id: int,
                  payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """启用 / 停用来源（停用不等于删除，不删影片与观看记录）。"""
    from .. import ingest
    from ..db import db
    enabled = bool((payload or {}).get("available"))
    with db() as conn:
        return _check(ingest.set_source_enabled(conn, source_id, enabled))


@router.delete("/sources/{source_id}")
def source_delete(source_id: int) -> Dict[str, Any]:
    """移除来源（不删影片、不删观看记录）。"""
    from .. import ingest
    from ..db import db
    with db() as conn:
        return _check(ingest.delete_source(conn, source_id))


@router.post("/ingest/import/text")
def import_text(body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """从一段文本批量提取番号并建档（论坛帖子 / Excel 复制的区域均可）。

    - 文本支持任意混排：番号识别复用本地文件扫描同一套规则（标准 / FC2 /
      HEYZO / 市场番号 / 日期番号 / TokyoHot），Excel 复制的区域（制表符分隔）
      直接粘贴即可，每行第一个番号生效；
    - 已存在的番号跳过并返回其影片 id，便于直接查看；
    - 新建影片 title 先用番号占位，勾选「自动刮削」后入队补全元数据与封面。
    """
    from .. import parser
    from ..db import db
    from ..movie_identity import norm_code
    from ..scraper import queue_movie_scrape

    text = str((body or {}).get("text") or "")
    scrape = bool((body or {}).get("scrape", True))
    if not text.strip():
        raise HTTPException(status_code=400, detail="文本为空")
    codes = parser.extract_codes(text)
    added: List[Dict[str, Any]] = []
    existed: List[Dict[str, Any]] = []
    with db() as conn:
        for code in codes:
            norm = norm_code(code)
            row = conn.execute(
                "SELECT id FROM movies WHERE code_norm=?", (norm,)).fetchone()
            if row:
                existed.append({"code": code, "movie_id": int(row["id"])})
                continue
            key = code.upper()
            conn.execute(
                "INSERT OR IGNORE INTO movies(key, code, code_norm, has_code, code_rule, title) "
                "VALUES(?,?,?,1,'import',?)", (key, code, norm, code))
            row2 = conn.execute("SELECT id FROM movies WHERE key=?", (key,)).fetchone()
            if not row2:
                continue
            mid = int(row2["id"])
            added.append({
                "code": code, "movie_id": mid,
                "scrape": queue_movie_scrape(mid) if scrape else "",
            })
    return {"ok": True, "added": added, "existed": existed, "total": len(codes)}


@router.get("/sources/{source_id}/check")
def source_check(source_id: int) -> Dict[str, Any]:
    """异步验证来源可用性；立即返回 checking，前端 1 秒轮询来源列表看结果。"""
    from .. import stream_verify
    return _check(stream_verify.submit_check(source_id))


@router.post("/sources/{source_id}/refresh")
def source_refresh(source_id: int,
                   payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """重新获取播放地址：可带新的 play_url / page_url，否则回源站页面提取。"""
    from .. import stream_verify
    p = payload or {}
    return _check(stream_verify.submit_refresh(
        source_id, str(p.get("play_url") or ""), str(p.get("page_url") or "")))


@router.get("/sources")
def sources_list(status: str = Query(default=""), q: str = Query(default=""),
                 page: int = Query(default=1, ge=1),
                 size: int = Query(default=50, ge=1, le=200)) -> Dict[str, Any]:
    """全库来源列表（维护 → 来源体检），支持状态 / 关键字筛选与分页。"""
    from .. import ingest
    from ..db import db
    with db() as conn:
        return ingest.list_all_sources(conn, status=status, q=q, page=page, size=size)


@router.post("/sources/batch")
def sources_batch(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """批量验证 / 重新获取 / 启用 / 停用（单次最多 200 条；不做批量删除）。"""
    from .. import ingest, stream_verify
    from ..db import db
    p = payload or {}
    action = str(p.get("action") or "").strip().lower()
    ids: List[int] = []
    for x in (p.get("ids") or [])[:200]:
        try:
            sid = int(x)
        except (TypeError, ValueError):
            continue
        if sid > 0:
            ids.append(sid)
    if not ids:
        raise HTTPException(status_code=400, detail="请先选择来源")
    if action not in ("check", "refresh", "enable", "disable"):
        raise HTTPException(status_code=400, detail="未知操作")
    queued = done = skipped = 0
    if action in ("check", "refresh"):
        for sid in ids:
            res = (stream_verify.submit_check(sid) if action == "check"
                   else stream_verify.submit_refresh(sid))
            if res.get("ok") and res.get("queued"):
                queued += 1
            else:
                skipped += 1
    else:
        with db() as conn:
            for sid in ids:
                res = ingest.set_source_enabled(conn, sid, action == "enable")
                if res.get("ok"):
                    done += 1
                else:
                    skipped += 1
    return {"ok": True, "action": action, "queued": queued, "done": done,
            "skipped": skipped}


@router.post("/ingest/source-failed")
def source_failed(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """播放器上报在线来源失败：自动切到下一可用来源，让前端重建播放地址。"""
    from .. import stream_verify
    p = payload or {}
    try:
        movie_id = int(p.get("movie_id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="缺少影片编号")
    if not movie_id:
        raise HTTPException(status_code=400, detail="缺少影片编号")
    return _check(stream_verify.report_failure(movie_id, str(p.get("reason") or ""), switch=True))
