# -*- coding: utf-8 -*-
"""把一部影片的字幕汇总成纯文本，供 AI 剧情分析使用。

优先顺序：
1. 内嵌**中文文本轨**（最准，零成本，且是用户实际看到的字幕）
2. 内嵌任意文本轨（日文 / 英文也可以交给模型）
3. 外挂字幕文件（.srt / .ass / .vtt / .txt）

图形字幕（PGS）与硬字幕都没有可直接读取的文字，返回空，由调用方回退到
「按元数据生成」并明确告知用户。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Tuple

from . import subprobe

MAX_CHARS = 60000          # 送入 AI 的上限（约 2 部正常影片的字幕量）
_ASS_TAG = re.compile(r"\{[^}]*\}")
_HTML_TAG = re.compile(r"<[^>]+>")


def _read_text_file(path: str) -> str:
    """读取外挂字幕：按扩展名决定解析方式。"""
    try:
        if not os.path.isfile(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read(2_000_000)
    except OSError:
        return ""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".ass", ".ssa"):
        return _ass_text(raw)
    if ext in (".srt", ".vtt", ".sub", ".smi", ".txt"):
        return subprobe.srt_to_text(raw)
    return ""


def _ass_text(raw: str) -> str:
    out: List[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s.lower().startswith("dialogue:"):
            continue
        parts = s.split(",", 9)
        if len(parts) < 10:
            continue
        text = _ASS_TAG.sub("", parts[9]).replace("\\N", " ").replace("\\n", " ")
        text = _HTML_TAG.sub("", text).strip()
        if text:
            out.append(text)
    return _dedupe(out)


def _dedupe(lines: List[str]) -> str:
    """去掉相邻重复行（很多字幕会为了特效重复两遍）。"""
    out: List[str] = []
    prev = ""
    for line in lines:
        s = line.strip()
        if not s or s == prev:
            continue
        out.append(s)
        prev = s
    return "\n".join(out)


def _track_priority(t: Dict[str, Any]) -> Tuple[int, int]:
    """数字越小越优先：中文 > 其他语言；文本 > 图形。"""
    zh = 1 if t.get("zh") else 0
    bad = 1 if (t.get("forced") or t.get("partial")) else 0
    return (0 if zh else 1, bad)


def collect(files: List[Dict[str, Any]], sidecar: List[Dict[str, Any]],
            cfg: Dict[str, Any]) -> Dict[str, Any]:
    """返回 {"text": str, "source": "embedded"|"sidecar"|"", "track": str, "chars": int}"""
    candidates: List[Tuple[Tuple[int, int], Dict[str, Any], Dict[str, Any]]] = []
    for f in files or []:
        path = f.get("path") or ""
        if not path or not os.path.isfile(path) or not f.get("sub_tracks"):
            continue
        try:
            info = json.loads(f["sub_tracks"])
        except (ValueError, TypeError):
            continue
        for t in (info.get("tracks") or []):
            if t.get("kind") != "text":
                continue
            candidates.append((_track_priority(t), f, t))
    candidates.sort(key=lambda x: x[0])

    for _, f, t in candidates:
        raw = subprobe.extract_track_text(f["path"], int(t.get("si") or 0), cfg)
        text = subprobe.srt_to_text(raw)
        if text:
            label = "%s%s" % (
                (t.get("lang") or "").upper() or "UND",
                (" · " + t["title"]) if t.get("title") else "",
            )
            return {"text": text[:MAX_CHARS], "source": "embedded",
                    "track": label.strip(" ·"), "chars": len(text)}

    # 外挂字幕：优先中文，其次任意
    sidecar = [s for s in (sidecar or []) if (s.get("path") or "")]
    sidecar.sort(key=lambda s: 0 if (s.get("lang") or "").lower().startswith("zh") else 1)
    for s in sidecar:
        text = _read_text_file(s["path"])
        if text:
            return {"text": text[:MAX_CHARS], "source": "sidecar",
                    "track": (s.get("lang") or "外挂字幕"), "chars": len(text)}

    return {"text": "", "source": "", "track": "", "chars": 0}
