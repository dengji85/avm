# -*- coding: utf-8 -*-
"""在线字幕匹配路由（P0：单部影片手动搜索 / 选择 / 下载）。

安全边界：前端只回传候选 id，不回传 URL；候选连同真实下载地址存在服务端内存中，
下载前再由 subsource 校验域名白名单，避免把后端当成任意 URL 的代理（SSRF）。
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, HTTPException

from .. import ext_components, subalign, subqc, subsource, subtitles
from .. import subtitle_sources
from ..config import DATA_DIR, load_config
from ..db import db, query_one

router = APIRouter()

_CAND_TTL = 600
_CAND_LOCK = threading.Lock()
_CANDIDATES: Dict[int, Tuple[float, Dict[str, Dict[str, Any]]]] = {}
# 在线候选预览：缓存内存中的完整 cue 列表（不落盘、不登记），随滚动分段返回，
# 后续页不再重复出站下载。条目上限防止长时间开着预览占内存。
_PREVIEW: Dict[str, Tuple[float, List[Tuple[float, float, str]], Dict[str, Any]]] = {}
_PREVIEW_MAX = 32

# 真实下载地址与源内部编号只留服务端；前端能看到的字段就这些
_PUBLIC_FIELDS = ("id", "provider", "provider_label", "name", "ext", "lang",
                  "duration_ms", "size", "estimate", "can_translate", "upload",
                  "downloads", "votes")

_LANG_TAG = {"简体中文": "zh-CN", "繁体中文": "zh-TW", "中文": "zh",
             "日中双语": "zh-ja", "日文": "ja", "英文": "en", "韩文": "ko",
             "俄文": "ru", "法文": "fr", "德文": "de", "西班牙文": "es",
             "葡萄牙文": "pt", "意大利文": "it", "泰文": "th", "越南文": "vi",
             "印尼文": "id", "阿拉伯文": "ar", "土耳其文": "tr",
             "未知": "und", "其他语言": "und"}


@router.get("/subtitles/sources")
def list_subtitle_sources() -> Dict[str, Any]:
    """设置页用：列出可选源、启用状态与凭据是否已配置（不回显任何密钥）。"""
    cfg = load_config(refresh=True)
    scfg = subsource._sub_cfg(cfg)
    enabled = {str(x) for x in (scfg.get("providers") or [])}
    items = []
    for name, label in subtitle_sources.LABELS.items():
        if name == "custom":
            continue
        cred = subtitle_sources.CREDENTIALS.get(name)
        items.append({
            "id": name, "label": label,
            "enabled": name in enabled,
            "needs_key": bool(cred),
            "configured": bool(scfg.get(cred[0])) if cred else True,
        })
    customs = []
    for i, s in enumerate(scfg.get("custom_sources") or []):
        if not isinstance(s, dict):
            continue
        customs.append({"index": i, "name": s.get("name") or "",
                        "url": s.get("url") or "", "root": s.get("root") or "",
                        "hosts": s.get("hosts") or "", "fields": s.get("fields") or {},
                        "enabled": bool(s.get("enabled", True))})
    return {"sources": items, "custom_enabled": "custom" in enabled,
            "custom_sources": customs}


def _save_candidates(movie_id: int, cands: list) -> None:
    with _CAND_LOCK:
        now = time.time()
        _CANDIDATES[movie_id] = (now, {c["id"]: c for c in cands})
        for key, (ts, _) in list(_CANDIDATES.items()):
            if now - ts > _CAND_TTL:
                _CANDIDATES.pop(key, None)


def _take_candidate(movie_id: int, cid: str) -> Optional[Dict[str, Any]]:
    with _CAND_LOCK:
        item = _CANDIDATES.get(movie_id)
        if not item or time.time() - item[0] > _CAND_TTL:
            return None
        return (item[1].get(cid) or {}).copy() or None


def _probe_duration(path: str, cfg: Dict[str, Any]) -> float:
    """元数据没写时长时用 ffprobe 现探，版本贴合评分依赖它。"""
    import subprocess

    try:
        out = subprocess.run(
            [ext_components.ffprobe_exe(cfg), "-v", "error",
             "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
        ).stdout.decode(errors="replace").strip()
        return float(out) if out else 0.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _movie_facts(conn, movie_id: int, cfg: Dict[str, Any]) -> Dict[str, Any]:
    row = query_one(conn, "SELECT code, runtime FROM movies WHERE id=?", (movie_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    code = (row["code"] or "").strip()
    seconds = float(row["runtime"] or 0) * 60
    if seconds <= 0:
        f = query_one(conn, "SELECT path FROM movie_files WHERE movie_id=? AND missing=0 "
                            "ORDER BY part LIMIT 1", (movie_id,))
        if f and f["path"] and os.path.isfile(f["path"]):
            seconds = _probe_duration(f["path"], cfg)
    return {"code": code, "video_seconds": seconds}


@router.post("/movies/{movie_id}/subtitles/online-search")
def online_search(movie_id: int, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """按影片番号搜索在线字幕候选；秒级 IO，任何单源失败都降级返回其余结果。"""
    cfg = load_config(refresh=True)
    with db() as conn:
        facts = _movie_facts(conn, movie_id, cfg)
    if not facts["code"]:
        raise HTTPException(status_code=400, detail="这部影片没有番号，无法在线匹配字幕")
    if not (cfg.get("subtitles") or {}).get("enabled", True):
        raise HTTPException(status_code=400, detail="在线字幕匹配已在配置中关闭")
    try:
        result = subsource.search_all(facts["code"], cfg, facts["video_seconds"])
    except Exception as exc:
        raise HTTPException(status_code=502, detail="字幕源查询失败：%s" % type(exc).__name__)
    cands = result["candidates"]
    _save_candidates(movie_id, cands)
    # 真实下载地址只留服务端：前端仅凭 id 采纳，避免 URL 被篡改
    public = [{k: c[k] for k in _PUBLIC_FIELDS if k in c} for c in cands]
    return {"ok": True, "code": facts["code"],
            "video_seconds": facts["video_seconds"],
            "candidates": public, "sources": result["sources"],
            "best_id": result.get("best_id", ""),
            "elapsed_ms": result.get("elapsed_ms", 0),
            "cached": bool(result.get("cached")),
            "hint": result.get("hint", "")}


@router.post("/movies/{movie_id}/subtitles/online-fetch")
def online_fetch(movie_id: int, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """采纳一条候选：下载 → 编码/时间轴归一 → 质检 → 落 data/subs → 登记。

    产物只写服务端 data/subs，不触碰用户影片目录，也不覆盖任何已有字幕。
    """
    cid = str((payload or {}).get("candidate_id") or "")
    cand = _take_candidate(movie_id, cid) if cid else None
    if not cand:
        raise HTTPException(status_code=404, detail="候选已过期，请重新搜索字幕")
    cfg = load_config(refresh=True)
    with db() as conn:
        facts = _movie_facts(conn, movie_id, cfg)
        dup = query_one(conn, "SELECT id, filename, file_path FROM movie_subtitles "
                              "WHERE movie_id=? AND origin_url=?",
                        (movie_id, cand.get("url") or ""))
        if dup and os.path.isfile(dup["file_path"] or ""):
            return {"ok": True, "sub_id": dup["id"], "filename": dup["filename"],
                    "duplicate": True, "score": 0, "notes": ["这条字幕已经下载过"]}
        if dup:
            # 记录还在但文件被删过（用户清理 / 磁盘变动），留着会永远返回失效
            # 的 sub_id 且无法重新下载，这里注销后按正常流程重下。
            conn.execute("DELETE FROM movie_subtitles WHERE id=?", (dup["id"],))
            conn.commit()

    try:
        raw = subsource.download(cand, cfg)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="字幕下载失败：%s" % exc)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="字幕源暂不可用：%s" % type(exc).__name__)

    code_tag = re.sub(r"[^A-Za-z0-9_-]", "", facts["code"]) or "movie"
    lang_tag = _LANG_TAG.get(cand.get("lang") or "未知", "und")
    ext = cand.get("ext") if cand.get("ext") in ("srt", "ass", "ssa", "vtt") else "srt"
    # 带上候选短哈希：同一来源同语言的多个版本不会互相覆盖
    filename = "%s-%s-%s-%s.%s" % (cand.get("provider") or "external", code_tag,
                                    lang_tag, cid[:6], ext)
    path = os.path.join(str(DATA_DIR), "subs", str(movie_id), filename)

    try:
        info = subqc.normalize_and_save(raw, path,
                                       video_seconds=facts["video_seconds"], cand=cand)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="字幕写入失败：%s" % exc)
    if info["score"] == 0:
        try:
            os.remove(path)
        except OSError:
            pass
        raise HTTPException(status_code=502, detail="下载内容不是有效字幕：" +
                            "；".join(info["notes"]))

    with db() as conn:
        added = subtitles._register_subtitle(
            conn, movie_id, path, source="external:" + (cand.get("provider") or "unknown"),
            lang=subqc.normalize_lang(cand.get("lang")),
            provider=cand.get("provider") or "",
            score=info["score"], origin_url=cand.get("url") or "",
            sub_duration_ms=info["sub_duration_ms"], auto_qa=False)
        conn.commit()
        row = query_one(conn, "SELECT id FROM movie_subtitles WHERE movie_id=? AND file_path=?",
                        (movie_id, path))
        sub_id = row["id"] if row else 0
    return {"ok": True, "sub_id": sub_id, "filename": filename,
            "duplicate": not added, "score": info["score"],
            "lang": info["lang_detected"], "cues": info["cues"],
            "encoding": info["encoding"], "repaired_lines": info["repaired_lines"],
            "can_translate": bool(cand.get("can_translate")),
            "notes": info["notes"]}


@router.post("/movies/{movie_id}/subtitles/online-preview")
def online_preview(movie_id: int,
                   payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """采纳前先看内容：下载到内存解析，不落盘、不登记、不消耗候选。

    冷门番号常有「标题像、内容其实是别的片」的字幕，光看文件名和评分
    判断不了；预览能直接读到台词再决定要不要采纳。结果短缓存，反复点
    预览不会重复出站。
    """
    payload = payload or {}
    cid = str(payload.get("candidate_id") or "")
    cand = _take_candidate(movie_id, cid) if cid else None
    if not cand:
        raise HTTPException(status_code=404, detail="候选已过期，请重新搜索字幕")
    try:
        offset = max(0, int(payload.get("offset") or 0))
        limit = max(1, min(200, int(payload.get("limit") or 60)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="分页参数不合法")
    keyword = str(payload.get("q") or "").strip().lower()[:80]
    key = "%d:%s" % (movie_id, cid)
    now = time.time()

    with _CAND_LOCK:
        hit = _PREVIEW.get(key)
        cached = bool(hit and now - hit[0] <= _CAND_TTL)
        for k, (ts, _, _) in list(_PREVIEW.items()):
            if now - ts > _CAND_TTL:
                _PREVIEW.pop(k, None)
        if len(_PREVIEW) >= _PREVIEW_MAX and not cached:
            oldest = min(_PREVIEW, key=lambda k: _PREVIEW[k][0], default=None)
            if oldest:
                _PREVIEW.pop(oldest, None)
    cues: List[Tuple[float, float, str]] = hit[1] if cached else []
    meta: Dict[str, Any] = hit[2] if cached else {}

    if not cached:
        cfg = load_config(refresh=True)
        try:
            raw = subsource.download(cand, cfg)
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="字幕预览失败：%s" % exc)
        except Exception as exc:
            raise HTTPException(status_code=502, detail="字幕源暂不可用：%s" % type(exc).__name__)
        cues = subqc.cues_from_bytes(raw, cand.get("ext") or "")
        if not cues:
            raise HTTPException(status_code=502, detail="下载内容不是有效字幕，无法预览")
        meta = {"name": cand.get("name") or "", "lang": cand.get("lang") or ""}
        with _CAND_LOCK:
            _PREVIEW[key] = (time.time(), cues, meta)

    if keyword:
        found = [(i, s, e, t) for i, (s, e, t) in enumerate(cues)
                 if keyword in (t or "").lower()]
        picked = found[:limit]
        return {"ok": True, **meta, "total": len(cues), "matched": len(found),
                "items": [{"index": i, "start": round(s, 2), "end": round(e, 2), "text": t}
                          for i, s, e, t in picked], "cached": True}
    page = [(i, *cues[i]) for i in range(offset, min(len(cues), offset + limit))]
    return {"ok": True, **meta, "total": len(cues),
            "items": [{"index": i, "start": round(s, 2), "end": round(e, 2), "text": t}
                      for i, s, e, t in page],
            "cached": cached}


def _num(payload: Dict[str, Any], key: str, default: float,
         lo: float, hi: float) -> float:
    try:
        v = float(payload.get(key, default) or default)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="参数格式不合法：%s" % key)
    return max(lo, min(hi, v))


@router.patch("/movies/{movie_id}/subtitles/{sub_id}/sync")
def save_subtitle_sync(movie_id: int, sub_id: int,
                       payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """保存本片字幕校准：时间轴平移 / 帧率缩放 / 屏幕垂直位置。

    只写元数据，不改动字幕文件本身；下次生成 VTT 时由后端统一应用，
    因此其它设备、AI 剧情问答拿到的时间轴也是一致的。
    """
    payload = payload or {}
    offset_ms = int(_num(payload, "offset_ms", 0, -600000, 600000))
    scale = _num(payload, "scale", 1.0, 0.90, 1.10)
    anchor_sec = _num(payload, "anchor_sec", 0, 0, 36000)
    pos_y = int(_num(payload, "pos_y", 0, -40, 40))
    method = str(payload.get("method") or "manual")[:32]
    confidence = _num(payload, "confidence", 0, 0, 1)
    with db() as conn:
        row = query_one(conn, "SELECT id FROM movie_subtitles WHERE id=? AND movie_id=?",
                        (sub_id, movie_id))
        if not row:
            raise HTTPException(status_code=404, detail="字幕不存在")
        conn.execute(
            "INSERT INTO subtitle_sync(sub_id, part, offset_ms, scale, anchor_sec, pos_y, "
            "method, confidence, updated_at) VALUES(?,?,?,?,?,?,?,?,datetime('now','localtime')) "
            "ON CONFLICT(sub_id) DO UPDATE SET offset_ms=excluded.offset_ms, "
            "scale=excluded.scale, anchor_sec=excluded.anchor_sec, pos_y=excluded.pos_y, "
            "method=excluded.method, confidence=excluded.confidence, "
            "updated_at=excluded.updated_at",
            (sub_id, int(payload.get("part") or 0), offset_ms, scale, anchor_sec,
             pos_y, method, confidence))
        conn.commit()
    return {"ok": True, "sub_id": sub_id, "offset_ms": offset_ms,
            "scale": scale, "anchor_sec": anchor_sec, "pos_y": pos_y,
            "method": method, "confidence": confidence}


@router.post("/movies/{movie_id}/subtitles/{sub_id}/sync/suggest")
def suggest_subtitle_sync(movie_id: int, sub_id: int,
                          payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """自动校准建议（只读）：以本地对白时间轴 / 内嵌字幕轨为参照做信号对齐。

    低置信度时只返回原因，不给可用数值——错误校准比不校准更糟。
    """
    with db() as conn:
        row = query_one(conn, "SELECT id FROM movie_subtitles WHERE id=? AND movie_id=?",
                        (sub_id, movie_id))
    if not row:
        raise HTTPException(status_code=404, detail="字幕不存在")
    try:
        return subalign.suggest(movie_id, sub_id, load_config(refresh=True))
    except Exception as exc:
        return {"suggested": False, "offset_ms": 0, "scale": 1.0,
                "anchor_sec": 0.0, "confidence": 0.0, "method": "none",
                "evidence": "", "scale_only": False,
                "reason": "自动检测失败：%s" % type(exc).__name__}


@router.get("/movies/{movie_id}/subtitles/{sub_id}/cues")
def list_subtitle_cues(movie_id: int, sub_id: int, offset: int = 0,
                       limit: int = 40, q: str = "", full: int = 0) -> Dict[str, Any]:
    """列出字幕的原始时间轴条目。

    两个用途：播放器「挑一句做校准锚点」（短文本、单行显示），以及详情页
    「阅读字幕」（full=1 保留完整台词）。后者不截断，否则长台词会被砍掉。
    """
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    text_limit = 2000 if full else 80
    with db() as conn:
        row = query_one(conn, "SELECT file_path FROM movie_subtitles WHERE id=? AND movie_id=?",
                        (sub_id, movie_id))
    if not row or not os.path.isfile(row["file_path"]):
        raise HTTPException(status_code=404, detail="字幕文件不存在")
    try:
        cues = subalign.subtitle_cues(row["file_path"])
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="字幕解析失败")
    total = len(cues)
    keyword = (q or "").strip().lower()
    items: List[Dict[str, Any]] = []
    if keyword:
        for i, (start, end, text) in enumerate(cues):
            if keyword in (text or "").lower():
                items.append({"index": i, "start": round(start, 2),
                              "end": round(end, 2), "text": (text or "")[:text_limit]})
                if len(items) >= limit:
                    break
        return {"ok": True, "total": total, "matched": len(items), "items": items}
    for i in range(offset, min(total, offset + limit)):
        start, end, text = cues[i]
        items.append({"index": i, "start": round(start, 2),
                      "end": round(end, 2), "text": (text or "")[:text_limit]})
    return {"ok": True, "total": total, "items": items}


@router.post("/movies/{movie_id}/subtitles/{sub_id}/sync/anchor")
def anchor_subtitle_sync(movie_id: int, sub_id: int,
                         payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """一键锚点：把字幕第 N 条对齐到当前播放时间。

    用户播到正片第一句台词时点一下即可，不依赖 ASR、不做文本比对——对
    「片头广告长度未知 / 自动对齐信号不足」的片源，这是唯一可靠的入口。
    只返回算好的 offset，不写库；确认后仍走 PATCH sync 保存。
    """
    payload = payload or {}
    try:
        video_time = float(payload.get("video_time") or 0)
        scale = float(payload.get("scale") or 1.0)
        index = int(payload.get("cue_index") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="参数格式不合法")
    if video_time <= 0:
        raise HTTPException(status_code=400, detail="还没开始播放，取不到当前画面时间")
    scale = max(0.90, min(1.10, scale))
    with db() as conn:
        row = query_one(conn, "SELECT file_path FROM movie_subtitles WHERE id=? AND movie_id=?",
                        (sub_id, movie_id))
    if not row or not os.path.isfile(row["file_path"]):
        raise HTTPException(status_code=404, detail="字幕文件不存在")
    try:
        cues = subalign.subtitle_cues(row["file_path"])
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="字幕解析失败")
    if not cues:
        raise HTTPException(status_code=400, detail="字幕没有可用的时间轴")
    if index < 0 or index >= len(cues):
        raise HTTPException(status_code=400,
                            detail="字幕条目序号超出范围（共 %d 条）" % len(cues))
    start, _end, text = cues[index]
    # 先判原始值再钳制：钳制会把「选错条目」的信号抹平（-2600s 被压成 -600s，
    # 看上去还挺合理），结果坏值照样存进库。
    raw_offset_ms = int(round((video_time - start * scale) * 1000))
    if abs(raw_offset_ms) > 600000:
        raise HTTPException(
            status_code=400,
            detail="这条字幕对不上：算出的偏移超过 10 分钟。请确认选的是画面里正在显示的那一句，"
                   "而不是后面重复出现的同一句台词")
    offset_ms = raw_offset_ms
    # 选了后段条目当锚点时，整条字幕会被推到片头之前：「这条对了、全片都错」
    mapped_last = cues[-1][1] * scale + offset_ms / 1000.0
    if mapped_last <= 5:
        raise HTTPException(status_code=400,
                            detail="这条字幕对不上：整条字幕会被推到片头之前，请换一条更靠前的字幕")
    return {"ok": True, "offset_ms": offset_ms, "scale": scale,
            "confidence": 0.9, "method": "anchor",
            "cue": {"index": index, "start": round(start, 2),
                    "text": (text or "")[:60]},
            "evidence": "第 %d 条字幕（%.2fs）对齐到当前画面 %.2fs"
                        % (index + 1, start, video_time)}
