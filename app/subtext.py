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
MAX_CUES = 4000            # 问答检索的分句上限
_ASS_TAG = re.compile(r"\{[^}]*\}")
_HTML_TAG = re.compile(r"<[^>]+>")
_TS_LINE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def parse_srt_cues(raw: str) -> List[Tuple[float, str]]:
    """SRT / VTT → [(起始秒, 文本)]，用于「剧情问答」按时间戳引用。"""
    cues: List[Tuple[float, str]] = []
    start: float | None = None
    buf: List[str] = []
    for line in (raw or "").lstrip("\ufeff").splitlines():
        s = line.strip()
        if "-->" in s:
            if start is not None and buf:
                cues.append((start, " ".join(buf).strip()))
            buf, start = [], None
            m = _TS_LINE.search(s)
            if m:
                h, mi, sec, ms = (int(x) for x in m.groups())
                start = h * 3600 + mi * 60 + sec + ms / 1000.0
            continue
        if not s or s.isdigit() or s.startswith(("WEBVTT", "NOTE", "STYLE", "Kind:", "Language:")):
            continue
        buf.append(_HTML_TAG.sub("", s))
    if start is not None and buf:
        cues.append((start, " ".join(buf).strip()))
    return [(t, x) for t, x in cues if x]


def _ass_cues(raw: str) -> List[Tuple[float, str]]:
    cues: List[Tuple[float, str]] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s.lower().startswith("dialogue:"):
            continue
        parts = s.split(",", 9)
        if len(parts) < 10:
            continue
        m = re.match(r"\s*(\d+):(\d{2}):(\d{2})[.:](\d{1,2})", parts[1])
        if not m:
            continue
        h, mi, sec, cs = (int(x) for x in m.groups())
        text = _ASS_TAG.sub("", parts[9]).replace("\\N", " ").replace("\\n", " ").strip()
        text = _HTML_TAG.sub("", text)
        if text:
            cues.append((h * 3600 + mi * 60 + sec + cs / 100.0, text))
    return cues


def _read_text_file(path: str) -> str:
    """读取外挂字幕：按扩展名决定解析方式。"""
    try:
        if not os.path.isfile(path):
            return ""
        # utf-8-sig：容忍 Windows 记事本/字幕软件写的 BOM
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
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


def _is_zh(lang: str) -> bool:
    """外挂字幕的语言可能是「简体中文」这类中文标签，也可能是 zh-CN，都要认。"""
    s = str(lang or "").lower()
    return ("中文" in s) or s.startswith(("zh", "cn", "chs", "cht"))


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
    sidecar.sort(key=lambda s: 0 if _is_zh(s.get("lang") or "") else 1)
    for s in sidecar:
        text = _read_text_file(s["path"])
        if text:
            return {"text": text[:MAX_CHARS], "source": "sidecar",
                    "track": (s.get("lang") or "外挂字幕"), "chars": len(text)}

    return {"text": "", "source": "", "track": "", "chars": 0}


def collect_cues(files: List[Dict[str, Any]], sidecar: List[Dict[str, Any]],
                 cfg: Dict[str, Any]) -> Dict[str, Any]:
    """取字幕分句（带时间码），供「剧情问答」按时间戳引用。"""
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
            if t.get("kind") == "text":
                candidates.append((_track_priority(t), f, t))
    candidates.sort(key=lambda x: x[0])

    for _, f, t in candidates:
        raw = subprobe.extract_track_text(f["path"], int(t.get("si") or 0), cfg)
        cues = parse_srt_cues(raw)[:MAX_CUES]
        if cues:
            label = ((t.get("lang") or "").upper() or "UND")
            if t.get("title"):
                label += " · " + t["title"]
            return {"cues": cues, "source": "embedded", "track": label, "count": len(cues)}

    sidecar = [s for s in (sidecar or []) if (s.get("path") or "")]
    sidecar.sort(key=lambda s: 0 if _is_zh(s.get("lang") or "") else 1)
    for s in sidecar:
        path = s["path"]
        try:
            if not os.path.isfile(path):
                continue
            # utf-8-sig：容忍 Windows 字幕文件的 BOM
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                raw = f.read(4_000_000)
        except OSError:
            continue
        cues = (_ass_cues(raw) if os.path.splitext(path)[1].lower() in (".ass", ".ssa")
                else parse_srt_cues(raw))[:MAX_CUES]
        if cues:
            return {"cues": cues, "source": "sidecar",
                    "track": (s.get("lang") or "外挂字幕"), "count": len(cues)}

    return {"cues": [], "source": "", "track": "", "count": 0}
