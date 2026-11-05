# -*- coding: utf-8 -*-
"""字幕转 WebVTT：浏览器 `<track>` 只认 VTT，SC/ASS 都要转。

SRT 的逗号毫秒改成点；ASS 逐行解析 Dialogue 的时间与文本。
转换结果可缓存（播放器与界面共用，见 api.py 的 vtt 接口）。
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

_TS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
_ASS_DIALOG = re.compile(r"^Dialogue:\s*[^,]*,[^,]*,([^,]*),([^,]*),([^,]*),", re.IGNORECASE)

# ------------------------------------------------------------------ 编码 / 脏时间轴归一
# 外部字幕站（迅雷 / SubtitleCat / 字幕组）的 SRT 常见 GBK、UTF-16、全角冒号、
# 单横线 "->"、毫秒 4 位、零宽空格、\N 换行残留。归一化放在消费端（生成 VTT 前），
# 用户手动放进来的老字幕也能一起被救活，而不只惠及在线下载的字幕。
_FULL2HALF = str.maketrans("０１２３４５６７８９：．，－＞　",
                          "0123456789:.,-> ")
_ZERO_WIDTH = re.compile("[\u200b\u200c\u00ad\ufeff]")
_TS_LINE = re.compile(r"^\s*(.+?)\s*-+>\s*(.+?)\s*$")
_TS_PART = re.compile(r"^(\d{1,2}):(\d{1,2}):(\d{1,2})[.,](\d{1,4})$")
_TS_PART_DUP_MIN = re.compile(r"^(\d{1,2}):(\d{1,2}):(\d{1,2}):(\d{1,2})[.,](\d{1,4})$")
_AD_LINE = re.compile(r"^\s*(https?://|www\.|字幕由|更多字幕请)", re.I)


def _decode_quality(text: str) -> int:
    """给一次解码结果打分：常见汉字/假名/ASCII 加分，乱码高发区字符重罚。"""
    good = bad = 0
    for ch in text[:8000]:
        o = ord(ch)
        if o < 128 or 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF:
            good += 1
        elif (0x3400 <= o <= 0x4DBF or 0xE000 <= o <= 0xF8FF
              or 0x80 <= o <= 0x24F or 0x2500 <= o <= 0x27BF):
            bad += 1
    return good - bad * 3


def decode_bytes(raw: bytes) -> Tuple[str, str]:
    """把任意来源的字幕字节解成文本，返回 (文本, 编码名)。

    UTF-8 严格解码优先；失败时在 GB18030 / Shift-JIS / Big5 里选「最像正常文字」
    的那个（几个编码都能解出任意字节，必须比质量而不是谁先成功）。
    """
    if not raw:
        return "", "empty"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace"), "utf-8-bom"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace"), "utf-16"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    best: Optional[Tuple[int, str, str]] = None
    for enc in ("gb18030", "cp932", "big5"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        q = _decode_quality(text)
        if best is None or q > best[0]:
            best = (q, enc, text)
    if best is not None:
        return best[2], best[1]
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def _fmt_ts(h: str, m: str, s: str, ms: str) -> str:
    return "%02d:%02d:%02d,%s" % (int(h), int(m), int(s), (ms + "000")[:3])


def _parse_ts(part: str) -> Optional[str]:
    """单个时间码 → 标准 SRT 写法；解析不了返回 None（调用方原样保留该行）。"""
    part = part.strip()
    m = _TS_PART_DUP_MIN.match(part)
    if m and m.group(2) == m.group(3):
        # 第三方转写工具会把分钟重复一遍（02:12:12:50.280 → 02:12:50,280），
        # 只在重复段确实相等时折叠，否则宁可不猜。
        return _fmt_ts(m.group(1), m.group(2), m.group(4), m.group(5))
    m = _TS_PART.match(part)
    if m:
        return _fmt_ts(*m.groups())
    return None


def normalize_timeline(text: str) -> Tuple[str, int]:
    """收敛非标准时间轴 / 换行残留，返回 (文本, 修复的时间轴行数)。

    只改能完整解析成时间轴的行，正文除 ASS 换行残留与纯广告行外一字不动。
    """
    out: List[str] = []
    fixed = 0
    for line in (text or "").splitlines():
        probe = _ZERO_WIDTH.sub("", line.translate(_FULL2HALF))
        m = _TS_LINE.match(probe)
        if m:
            left, right = _parse_ts(m.group(1)), _parse_ts(m.group(2))
            if left and right:
                if line.strip() != (left + " --> " + right):
                    fixed += 1
                out.append(left + " --> " + right)
                continue
        # 非时间轴行：正文原样，只处理 ASS 换行残留与纯广告行
        if _AD_LINE.match(line):
            continue
        out.append(line.replace("\\N", "\n").replace("\\n", "\n"))
    return "\n".join(out), fixed


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
    """SRT / 纯文本时间轴字幕 → WebVTT（先收敛脏时间轴，再转时间码）。"""
    text = (text or "").lstrip("\ufeff")      # Windows 记事本存的无 BOM 头
    text, _ = normalize_timeline(text)
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
        # 外部字幕站的产物常见 GBK / UTF-16 / 非标准时间轴：统一在解码层收敛，
        # 避免 utf-8-sig + errors=replace 把整条中文字幕静默换成乱码方块。
        with open(path, "rb") as f:
            raw, _ = decode_bytes(f.read(4_000_000))
    except OSError:
        return ""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".ass", ".ssa"):
        return ass_to_vtt(raw)
    return srt_to_vtt(raw)
