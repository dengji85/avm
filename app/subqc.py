# -*- coding: utf-8 -*-
"""在线字幕候选的归一化、去重与可解释评分。

P0 只做「下载前预估 + 下载后质检」两件事：搜索阶段拿不到字幕正文，只能按
语言 / 时长 / 来源估分；下载后才有 cue 密度、乱码率、文本健康这些真信号。
所有分数都附 notes，前端能直接告诉用户为什么这条被推荐或被打低分。
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Dict, List, Tuple

from . import vtt

_CUE_TS = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
_ASS_MARK = re.compile(r"(?:\[Script Info\]|^Dialogue:)", re.I | re.M)
_LANG_POINTS = {
    "简体中文": 30, "日中双语": 26, "繁体中文": 24,
    "中文": 22, "日文": 18, "英文": 10, "未知": 4,
}
_TRUST = {"subtitlecat": 8, "thunder": 5, "local": 10, "embedded": 10}


def _sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int((ms + "000")[:3]) / 1000.0


def parse_cues(text: str) -> List[Tuple[float, float, str]]:
    """SRT → [(起始秒, 结束秒, 文本)]；只认规范时间轴（归一化后必然是规范写法）。"""
    cues: List[Tuple[float, float, str]] = []
    start = end = 0.0
    buf: List[str] = []
    for line in (text or "").splitlines():
        m = _CUE_TS.search(line)
        if m:
            if buf and end > start:
                cues.append((start, end, " ".join(x.strip() for x in buf if x.strip())))
            start = _sec(*m.groups()[:4])
            end = _sec(*m.groups()[4:])
            buf = []
            continue
        s = line.strip()
        if not s or s.isdigit() or s.startswith(("WEBVTT", "NOTE", "STYLE")):
            continue
        buf.append(re.sub(r"<[^>]+>", "", s))
    if buf and end > start:
        cues.append((start, end, " ".join(x.strip() for x in buf if x.strip())))
    return [c for c in cues if c[2]]


def detect_lang(text: str) -> str:
    """按字形粗判语言；只用于候选标注与评分，不改写任何文本。"""
    sample = text[:6000]
    if not sample:
        return "未知"
    han = sum(1 for c in sample if "\u4e00" <= c <= "\u9fff")
    kana = sum(1 for c in sample if "\u3040" <= c <= "\u30ff")
    latin = sum(1 for c in sample if c.isascii() and c.isalpha())
    trad = sum(1 for c in sample if c in "體臺灣與為這將會說話時對過還裡個們來")
    simp = sum(1 for c in sample if c in "体台湾与为这将说话时对过还里个们来")
    if kana > 20 and han > 20:
        return "日中双语"
    if han > 30:
        return "繁体中文" if trad > simp else "简体中文"
    if kana > 20:
        return "日文"
    if latin > 30:
        return "英文"
    return "未知"


def _fit_points(last_end: float, video_seconds: float, notes: List[str]) -> Tuple[int, bool]:
    """版本贴合：字幕末条时间 vs 本地视频时长，是判断「是不是同一版本」最强的信号。"""
    if video_seconds <= 0:
        notes.append("缺少视频时长，未做版本贴合校验")
        return 15, True
    dev = abs(last_end - video_seconds) / video_seconds
    if dev <= 0.03:
        return 25, True
    if dev <= 0.08:
        notes.append("字幕时长与影片相差约 %.0f%%" % (dev * 100))
        return 18, True
    if dev <= 0.15:
        notes.append("字幕时长偏差 %.0f%%，可能是另一个压制版本" % (dev * 100))
        return 10, True
    notes.append("字幕时长偏差 %.0f%%，疑似配错版本" % (dev * 100))
    return 0, False


def analyze(text: str, *, video_seconds: float = 0, lang: str = "",
            provider: str = "", encoding: str = "utf-8",
            repaired: int = 0) -> Dict[str, Any]:
    """下载后质检，返回分数、扣分原因与可登记的字幕元数据。"""
    cues = parse_cues(text)
    notes: List[str] = []
    if not cues:
        return {"score": 0, "notes": ["未解析出有效字幕行，文件可能不是字幕"],
                "cues": 0, "sub_duration_ms": 0, "lang_detected": "未知",
                "garbled_ratio": 1.0}
    joined = "\n".join(c[2] for c in cues)
    last_end = max(c[1] for c in cues)
    lang = lang or detect_lang(joined)
    lang_pts = _LANG_POINTS.get(lang, 4)
    fit_pts, _ = _fit_points(last_end, video_seconds, notes)

    minutes = max((video_seconds or last_end) / 60.0, 1.0)
    density = len(cues) / minutes
    if 6 <= density <= 45:
        density_pts = 15
    elif 3 <= density < 6 or 45 < density <= 70:
        density_pts = 8
        notes.append("字幕条数密度偏低/偏高（%.0f 条/分）" % density)
    else:
        density_pts = 3
        notes.append("字幕条数密度异常（%.0f 条/分），可能是错片或机翻碎片" % density)

    bad = sum(1 for c in joined
              if c == "\ufffd" or 0xE000 <= ord(c) <= 0xF8FF
              or (ord(c) < 32 and c not in "\r\n\t"))
    garbled = bad / max(len(joined), 1)
    texts = [c[2] for c in cues]
    dup = 1 - len(set(texts)) / max(len(texts), 1)
    avg_len = sum(len(t) for t in texts) / len(texts)
    health = 15
    if garbled > 0.01:
        health -= 7
        notes.append("检测到乱码字符（%.1f%%）" % (garbled * 100))
    # 对白场景短句重复是常态（同一句话会出现几十次），阈值放宽避免误报
    if dup > 0.45:
        health -= 4
        notes.append("重复台词较多（%.0f%%）" % (dup * 100))
    if avg_len < 2:
        health -= 3
        notes.append("平均句长过短，疑似机翻碎片")
    health = max(0, health)

    trust = _TRUST.get(provider, 5)
    repair = 5
    if encoding not in ("utf-8", "utf-8-bom"):
        repair -= 3
        notes.append("原始编码为 %s，已转成 UTF-8" % encoding)
    if repaired:
        repair -= 2
        notes.append("修复了 %d 行非标准时间轴" % repaired)

    score = max(0, min(100, lang_pts + fit_pts + density_pts + health + trust + repair))
    return {
        "score": score, "notes": notes, "cues": len(cues),
        "sub_duration_ms": int(last_end * 1000),
        "lang_detected": lang, "garbled_ratio": round(garbled, 5),
        "density": round(density, 2),
    }


def estimate_score(cand: Dict[str, Any], video_seconds: float) -> int:
    """下载前预估匹配度：只有语言 / 时长 / 来源可用，按可用维度归一到 100。"""
    lang_pts = _LANG_POINTS.get(cand.get("lang") or "未知", 4)
    trust = _TRUST.get(cand.get("provider") or "", 5)
    avail = 30 + 10
    got = lang_pts + trust
    if video_seconds > 0:
        avail += 25
        if (cand.get("duration_ms") or 0) > 0:
            fit, _ = _fit_points(cand["duration_ms"] / 1000.0, video_seconds, [])
            got += fit
        else:
            got += 15          # 拿不到字幕时长时给中性分，别凭空拔高匹配度
    return max(0, min(100, int(got / avail * 100)))


def dedupe(cands: List[Dict[str, Any]], limit: int = 24) -> List[Dict[str, Any]]:
    """按 url 去重；同一来源同名文件（迅雷常见一堆 ipx-343.srt）最多留 2 条。"""
    seen_url = set()
    seen_name: Dict[Tuple[str, str], int] = {}
    out: List[Dict[str, Any]] = []
    for c in cands:
        url = c.get("url") or ""
        if not url or url in seen_url:
            continue
        seen_url.add(url)
        key = (c.get("provider") or "", (c.get("name") or "").lower())
        n = seen_name.get(key, 0)
        if n >= 2:
            continue
        seen_name[key] = n + 1
        out.append(c)
        if len(out) >= limit:
            break
    return out


def normalize_and_save(raw: bytes, path: str, *, video_seconds: float = 0,
                       cand: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """下载字节 → 编码归一 + 时间轴修复 → UTF-8 落盘 → 质检。

    ASS/SSA 不走 normalize_timeline：它的 Dialogue 行靠逗号分列，插入换行会破坏结构。
    """
    cand = cand or {}
    text, encoding = vtt.decode_bytes(raw)
    repaired = 0
    if _ASS_MARK.search(text[:4000]):
        normalized = text
    else:
        normalized, repaired = vtt.normalize_timeline(text)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 临时文件 + 原子替换，避免下载中断留下半个字幕
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(normalized)
    os.replace(tmp, path)
    info = analyze(normalized, video_seconds=video_seconds,
                   lang=cand.get("lang") or "", provider=cand.get("provider") or "",
                   encoding=encoding, repaired=repaired)
    info["encoding"] = encoding
    info["repaired_lines"] = repaired
    info["sha1"] = hashlib.sha1(raw).hexdigest()
    info["path"] = path
    return info
