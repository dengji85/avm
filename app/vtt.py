# -*- coding: utf-8 -*-
"""字幕转 WebVTT：浏览器 `<track>` 只认 VTT，SC/ASS 都要转。

SRT 的逗号毫秒改成点；ASS 逐行解析 Dialogue 的时间与文本。
转换结果可缓存（播放器与界面共用，见 api.py 的 vtt 接口）。
"""
from __future__ import annotations

import os
import re
from typing import List, Tuple

_TS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
_ASS_DIALOG = re.compile(r"^Dialogue:\s*[^,]*,[^,]*,([^,]*),([^,]*),([^,]*),", re.IGNORECASE)


def _ts_to_vtt(h: str, m: str, s: str, ms: str) -> str:
    return "%02d:%02d:%02d.%s" % (int(h), int(m), int(s), (ms + "000")[:3])


def _ass_ts(t: str) -> str:
    """ASS 时间是 H:MM:SS.cc（百分秒），转成 VTT 的 H:MM:SS.mmm。"""
    m = re.match(r"\s*(\d+):(\d{2}):(\d{2})[.:](\d{1,2})", t or "")
    if not m:
        return "00:00:00.000"
    h, mi, s, cs = m.groups()
    return "%02d:%02d:%02d.%s" % (int(h), int(mi), int(s), (cs + "00")[:3])


def srt_to_vtt(text: str) -> str:
    """SRT / 纯文本时间轴字幕 → WebVTT（内容只做标签清理，不动时间码）。"""
    text = (text or "").lstrip("\ufeff")      # Windows 记事本存的无 BOM 头
    out: List[str] = ["WEBVTT", ""]
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    n = 0
    for block in blocks:
        lines = [l for l in block.splitlines() if l.strip()]
        if not any("-->" in l for l in lines):
            continue
        cue: List[str] = []
        for line in lines:
            if "-->" in line:
                line = _TS.sub(lambda m: _ts_to_vtt(*m.groups()), line)
                n += 1
                cue.append(str(n))
                cue.append(line)          # 时间轴行（毫秒逗号已换成点）
                continue
            if line.strip().isdigit():
                continue        # SRT 的序号行，VTT 里由我们重新编号
            cue.append(re.sub(r"<[^>]+>", "", line))
        if len(cue) >= 2:
            out.extend(cue)
            out.append("")
    return "\n".join(out).strip() + "\n"


def ass_to_vtt(text: str) -> str:
    """ASS/SSA → WebVTT：取 Dialogue 行与起止时间，去掉样式标签。"""
    out: List[str] = ["WEBVTT", ""]
    n = 0
    for line in text.splitlines():
        if not line.lower().startswith("dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        start, end, body = parts[1], parts[2], parts[9]
        body = re.sub(r"\{[^}]*\}", "", body)
        body = body.replace("\\N", "\n").replace("\\n", "\n").strip()
        if not body:
            continue
        n += 1
        out.append(str(n))
        out.append("%s --> %s" % (_ass_ts(start), _ass_ts(end)))
        out.append(body)
        out.append("")
    return "\n".join(out).strip() + "\n"


def file_to_vtt(path: str) -> str:
    """按扩展名选择转换器；读不到返回空串。"""
    try:
        if not os.path.isfile(path):
            return ""
        # utf-8-sig：Windows 下大量字幕带 BOM，直接用 utf-8 会把 BOM 当成首条内容
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            raw = f.read(4_000_000)
    except OSError:
        return ""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".ass", ".ssa"):
        return ass_to_vtt(raw)
    return srt_to_vtt(raw)
