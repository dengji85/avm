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
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Body, HTTPException

from .. import ext_components, subqc, subsource, subtitles
from ..config import DATA_DIR, load_config
from ..db import db, query_one

router = APIRouter()

_CAND_TTL = 600
_CAND_LOCK = threading.Lock()
_CANDIDATES: Dict[int, Tuple[float, Dict[str, Dict[str, Any]]]] = {}

_LANG_TAG = {"简体中文": "zh-CN", "繁体中文": "zh-TW", "中文": "zh",
             "日中双语": "zh-ja", "日文": "ja", "英文": "en", "未知": "und"}


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
    public = [{k: v for k, v in c.items() if k != "url"} for c in cands]
    return {"ok": True, "code": facts["code"],
            "video_seconds": facts["video_seconds"],
            "candidates": public, "sources": result["sources"]}


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
        dup = query_one(conn, "SELECT id, filename FROM movie_subtitles "
                              "WHERE movie_id=? AND origin_url=?",
                        (movie_id, cand.get("url") or ""))
        if dup:
            return {"ok": True, "sub_id": dup["id"], "filename": dup["filename"],
                    "duplicate": True, "score": 0, "notes": ["这条字幕已经下载过"]}

    try:
        raw = subsource.download(cand, cfg)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="字幕下载失败：%s" % exc)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="字幕源暂不可用：%s" % type(exc).__name__)

    code_tag = re.sub(r"[^A-Za-z0-9_-]", "", facts["code"]) or "movie"
    lang_tag = _LANG_TAG.get(cand.get("lang") or "未知", "und")
    ext = cand.get("ext") if cand.get("ext") in ("srt", "ass", "ssa", "vtt") else "srt"
    filename = "%s-%s-%s.%s" % (cand.get("provider") or "external", code_tag, lang_tag, ext)
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
            lang=cand.get("lang") or "", provider=cand.get("provider") or "",
            score=info["score"], origin_url=cand.get("url") or "",
            sub_duration_ms=info["sub_duration_ms"])
        conn.commit()
        row = query_one(conn, "SELECT id FROM movie_subtitles WHERE movie_id=? AND file_path=?",
                        (movie_id, path))
        sub_id = row["id"] if row else 0
    return {"ok": True, "sub_id": sub_id, "filename": filename,
            "duplicate": not added, "score": info["score"],
            "lang": info["lang_detected"], "cues": info["cues"],
            "encoding": info["encoding"], "repaired_lines": info["repaired_lines"],
            "notes": info["notes"]}
