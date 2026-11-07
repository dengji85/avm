# -*- coding: utf-8 -*-
"""自动字幕校准：语音/参考时间轴与字幕时间轴的信号对齐。

核心思路来自 ffsubsync，但按 AVM 的场景改了两处：

1. 不比文字，只比「什么时候有人在说话」——本地 ASR 是日语原文、下载字幕
   多是中文译文，任何文本/语义对齐都会失效，而语音活动是语言无关的。
2. 不依赖 numpy。0.5~1 秒分辨率下序列只有几千位，用 Python 大整数位集 +
   ``int.bit_count()`` 做互相关，零依赖、几毫秒，打包版不必带 numpy。

只输出「建议值 + 置信度」，绝不自动写入：批量校准时一次错误同步比不同步更糟。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple

from . import asr, subprobe, subqc, vtt
from .config import load_config
from .db import db, query_one

STEP = 1.0                 # 秒 / 格；字幕断句本身有 ±0.3~1s 抖动，10ms 是伪精度
MAX_LAG_SEC = 180          # 片头广告通常 0~120s，超过 180s 多半是配错片
SCALES = (1.0, 1.001, 0.999, 1.04, 0.96, 25 / 24, 24 / 25,
          23.976 / 25, 25 / 23.976)
MIN_CONFIDENCE = 0.6      # 低于此值连建议按钮都不给


def _bits_from_spans(spans: List[Tuple[float, float]], mode: str,
                     step: float = STEP) -> Tuple[int, int]:
    """把 [(start,end)] 编成位集。

    occupancy：整个区间置 1（ffsubsync 官方做法，对翻译断句鲁棒）；
    onset：只在起点附近置 1（对白密集时峰更尖，抗「整段全是 1」）。
    """
    if not spans:
        return 0, 0
    last = max(e for _, e in spans)
    n = int(last / step) + 4
    bits = 0
    for start, end in spans:
        if mode == "onset":
            i0 = max(0, int(start / step))
            i1 = min(n - 1, i0 + 1)
        else:
            i0 = max(0, int(start / step))
            i1 = min(n - 1, int(max(end, start + step) / step))
        if i1 >= i0:
            bits |= ((1 << (i1 - i0 + 1)) - 1) << i0
    return bits, n


def _resample(bits: int, ratio: float) -> int:
    """按 ratio 拉伸/压缩时间轴，用于帧率校正。"""
    if abs(ratio - 1.0) < 1e-9:
        return bits
    out = 0
    while bits:
        low = bits & -bits
        i = low.bit_length() - 1
        out |= 1 << max(0, int(i * ratio))
        bits ^= low
    return out


def best_shift(ref_bits: int, sub_bits: int, *, max_lag: int = None,
               step: float = STEP, scales=SCALES) -> Dict[str, Any]:
    """穷举 lag × scale，返回匹配数最高的位移及次峰（用于置信度）。

    评分沿用 ffsubsync：score = 2*matched - ref_count，等价于
    「两边都说是语音」减去「参考有语音但字幕没有」；不惩罚字幕比语音多显示一点。
    """
    max_lag = int(MAX_LAG_SEC / step) if max_lag is None else max_lag
    ref_count = ref_bits.bit_count()
    sub_density = sub_bits.bit_count() / max(sub_bits.bit_length(), 1)
    if not ref_count or not sub_bits:
        return {"offset_ms": 0, "scale": 1.0, "matched": 0, "runner_up": 0,
                "ref_count": ref_count, "sub_density": sub_density}
    best = (-1, 0, 0, 1.0)          # (score, lag, matched, scale)
    all_scores: List[Tuple[int, int]] = []
    best_scale = 1.0
    for ratio in scales:
        sb = _resample(sub_bits, ratio)
        for lag in range(-max_lag, max_lag + 1):
            shifted = (sb << lag) if lag >= 0 else (sb >> -lag)
            matched = (ref_bits & shifted).bit_count()
            all_scores.append((matched, lag))
            score = 2 * matched - ref_count
            if score > best[0]:
                best = (score, lag, matched, ratio)
    _, best_lag, peak, best_scale = best
    # 次峰要排除峰值邻域，否则相邻 lag 的匹配数几乎一样，锐度永远接近 1
    runner_up = max((m for m, l in all_scores if abs(l - best_lag) > 3), default=0)
    return {"offset_ms": int(round(best_lag * step * 1000)),
            "scale": round(best_scale, 6), "matched": peak,
            "runner_up": runner_up, "ref_count": ref_count,
            "sub_density": sub_density}


def assess(result: Dict[str, Any], cue_count: int) -> Tuple[float, str]:
    """质量门控：宁可不给建议，也不给一个错的建议。

    主判据是「对齐改善量」gain = 实际匹配 / 随机匹配的期望。用它而不是峰锐度：
    对白本身有周期性时次峰几乎和真峰一样高（实测 560 vs 556），按锐度会把
    正确结果误拒；而字幕与影片完全无关、或字幕几乎全程显示时，gain 都接近 1，
    用它能把这两种危险情况一起挡掉。
    """
    peak = result["matched"]
    second = result["runner_up"]
    ref_count = result["ref_count"]
    density = float(result.get("sub_density") or 0)
    if cue_count < 30 or ref_count < 40:
        return 0.0, "字幕或语音样本太少，无法可靠对齐"
    norm = peak / max(ref_count, 1)
    baseline = ref_count * density
    gain = peak / max(baseline, 1)
    sharp = peak / max(second, 1)
    if density > 0.85:
        return 0.0, "字幕几乎全程显示，时间轴没有可对齐的特征"
    if norm < 0.35:
        return 0.0, "对齐匹配率过低：字幕与影片可能不是同一版本，或本地对白数据太少"
    if gain < 1.25:
        return 0.0, "对齐没有显著优于随机匹配，结果不可信"
    # 参考信号稀疏时（如 ASR 只识别出少量对白），峰往往很平：实测某部片
    # 峰 599 / 次峰 595，相差不到 1%，任何 lag 都能「差不多对上」，这时的
    # 建议值基本是随机解。高覆盖率（norm≥0.75）才允许峰平，否则要求明显尖锐。
    if norm < 0.75 and sharp < 1.08:
        return 0.0, "对齐存在多个几乎等价的解（参考信号不足），建议手动对齐"
    if abs(result["offset_ms"]) > MAX_LAG_SEC * 1000:
        return 0.0, "偏移超出合理范围"
    if abs(result["scale"] - 1.0) > 0.05:
        return 0.0, "帧率缩放超出合理范围"
    conf = min(1.0, 0.35 + min(gain - 1.0, 1.0) * 0.45 + min(norm, 1.0) * 0.20)
    return round(conf, 3), ""


def subtitle_cues(path: str) -> List[Tuple[float, float, str]]:
    """公开入口：读任意编码的 SRT/ASS，返回原始时间轴的 [(start,end,text)]。"""
    return _subtitle_cues(path)


def _subtitle_cues(path: str) -> List[Tuple[float, float, str]]:
    """读任意编码的 SRT/ASS，统一转成 [(start,end,text)]。"""
    with open(path, "rb") as f:
        text, _ = vtt.decode_bytes(f.read(4_000_000))
    ext = os.path.splitext(path)[1].lower()
    if ext in (".ass", ".ssa"):
        built = vtt.ass_to_vtt(text)
    else:
        built = vtt.srt_to_vtt(text)
    return subqc.parse_cues(built)


def _reference_spans(movie_id: int, cfg: Dict[str, Any]) -> Tuple[List[Tuple[float, float]], str]:
    """降级链取参考时间轴：已跑过的 ASR → 内嵌文本轨 → 空。"""
    segs = asr.read_segments(movie_id)
    if segs:
        spans = [(float(s.get("start") or 0), float(s.get("end") or 0)) for s in segs]
        return [x for x in spans if x[1] > x[0]], "asr_activity"
    # 内嵌文本轨的时间轴天然正确（它就在本地视频里），且同样不用比文字。
    # ffmpeg 抽轨对 strm / 远程文件可能失败，失败就继续降级，不影响其它功能。
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT path, sub_tracks FROM movie_files WHERE movie_id=? AND missing=0",
                (movie_id,)).fetchall()
        for row in rows:
            try:
                info = json.loads(row["sub_tracks"] or "{}")
            except ValueError:
                continue
            for t in (info.get("tracks") or []):
                if t.get("kind") != "text" or not os.path.isfile(row["path"] or ""):
                    continue
                raw = subprobe.extract_track_text(row["path"], int(t.get("si") or 0), cfg)
                if not raw:
                    continue
                norm, _ = vtt.normalize_timeline(raw)
                cues = subqc.parse_cues(vtt.srt_to_vtt(norm))
                spans = [(c[0], c[1]) for c in cues]
                if len(spans) >= 30:
                    return spans, "embedded_track"
    except Exception:
        pass
    return [], "none"


def suggest(movie_id: int, sub_id: int, cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """给出校准建议（只读，不写库）。任何一步失败都降级，不抛异常给前端。"""
    cfg = cfg or load_config(refresh=True)
    base = {"suggested": False, "offset_ms": 0, "scale": 1.0, "anchor_sec": 0.0,
            "confidence": 0.0, "method": "none", "evidence": "", "reason": "",
            "scale_only": False}
    with db() as conn:
        row = query_one(conn, "SELECT file_path FROM movie_subtitles "
                              "WHERE id=? AND movie_id=?", (sub_id, movie_id))
    if not row or not os.path.isfile(row["file_path"]):
        base["reason"] = "字幕文件不存在"
        return base
    try:
        if os.path.getsize(row["file_path"]) == 0:
            base["reason"] = "字幕文件是空的，请先换一条字幕"
            return base
    except OSError:
        pass
    try:
        cues = _subtitle_cues(row["file_path"])
    except (OSError, ValueError) as exc:
        base["reason"] = "字幕解析失败：%s" % type(exc).__name__
        return base
    if len(cues) < 30:
        base["reason"] = "字幕条数太少，无法可靠对齐"
        return base
    sub_spans = [(c[0], c[1]) for c in cues]

    ref_spans, method = _reference_spans(movie_id, cfg)
    if not ref_spans:
        # 没有语音参考时，用户标过的片头结束点是很好的先验，但只给参考值、
        # 不给「应用」按钮，避免把广告长度当成可信校准写进库。
        with db() as conn:
            mv = query_one(conn, "SELECT skip_intro, runtime FROM movies WHERE id=?", (movie_id,))
        intro = float((mv["skip_intro"] if mv else 0) or 0)
        base["method"] = "intro_prior" if intro > 0 else "none"
        base["offset_ms"] = int(intro * 1000)
        base["evidence"] = ("已标记片头广告 %.1f 秒，可作为手动微调的起点" % intro
                            if intro > 0 else "")
        base["reason"] = ("没有可用的语音时间轴（可先提取对白，或手动微调）"
                          if not intro else "片头长度仅为参考值，请在播放中确认后再保存")
        return base

    rows = []
    for mode in ("occupancy", "onset"):
        rb, _ = _bits_from_spans(ref_spans, mode)
        sb, _ = _bits_from_spans(sub_spans, mode)
        r = best_shift(rb, sb)
        conf, reason = assess(r, len(cues))
        rows.append({"mode": mode, "result": r, "conf": conf, "reason": reason})
    top = max(rows, key=lambda x: x["conf"])
    best, best_mode = top["result"], top["mode"]
    best_conf, best_reason = top["conf"], top["reason"]
    if best_conf < MIN_CONFIDENCE:
        base["method"] = method
        base["reason"] = best_reason or "对齐结果置信度不足，请手动微调"
        # 这里不再给「帧率差」建议。offset 不确定时，联合搜索会为了凑匹配把 scale
        # 扭成一个伪解：实测某片给出 0.95904，而用「首条对正片开头、末条对视频末尾」
        # 两点约束解出的真实值是 1.0002——按伪解去调只会越调越乱。
        # 真要判断有没有帧率差，用「对好开头再看片尾」：片尾差几十秒才需要校正。
        return base

    src = "本地对白时间轴" if method == "asr_activity" else "内嵌字幕轨时间轴"
    mode_txt = "区间重合" if best_mode == "occupancy" else "起始沿"
    scale_txt = ("，帧率校正 %.4f" % best["scale"]) if abs(best["scale"] - 1.0) > 1e-4 else ""
    base.update({
        "suggested": True,
        "offset_ms": best["offset_ms"], "scale": best["scale"],
        "confidence": best_conf, "method": method,
        "evidence": "以%s为参照（%s），匹配 %d/%d 条%s" % (
            src, mode_txt, best["matched"], best["ref_count"], scale_txt),
    })
    return base
