# -*- coding: utf-8 -*-
"""广告片检测引擎（内容级，不依赖 AI / 不联网）。

要解决的问题
------------
很多片源在正片前拼了 1~10 分钟的宣传片、花絮、其他番号预告，或者整片都带着
盗版站的跑马灯 / 台标。这类片子观感极差，但文件名往往看不出来（番号正常、
体积正常），只能看画面。

判据（全部可解释、可复核）
--------------------------
1. **文字条帧比例**（主判据）
   宣传片 / 花絮 / 预告几乎总带整行大字幕或跑马灯，而正片极少有横贯画面的
   高对比文字行。于是分别统计三段的「文字条帧比例」：
      head（片头 0~240s）、body（片中 30%~70%，作为基准）、tail（片尾 110s）
   再比较「片头 vs 片中」：
      * 片头明显更高 → 片头是独立节目（广告 / 花絮）
      * 片中本身也高 → 整片带字幕条，属另一类问题，不当作片头广告
2. **全片跑马灯**：片中基准段底部条带若普遍有文字 → 整片带广告条。
3. **文件名推广域名**（弱信号）：`xxx.com@ABC-123.mp4`、`【…www.xxx.com】`
   —— 搬运组前缀很常见，不能单独定罪，仅加分。
4. **时长偏差**（弱信号）：真实时长明显长于刮削时长，常见于拼接广告。
5. **证据拼版**：不论分数高低，都把片头 / 片尾抽帧拼成一张缩略图墙（带时间
   戳、可疑帧标红）。人工一眼即可复核，这是「精确」的最终保障。

为什么不用 AI 视觉：项目现有 AI 只有文本通道；抽帧 + 文字结构统计在真实样本上
区分度已经够用（片头 0.9 vs 片中 0.07），本地零成本、可解释。

为什么不用 blackdetect：整片扫描太贵（4K 片段 180s 要 20s+），而文字条判据在同
样场景下更准，成本只有逐点 seek 抽帧（1080p 约 0.2s/帧，4 路并发）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import DATA_DIR, load_config

# 证据图落盘目录（data/adcheck/<movie_id>.jpg）
SHEET_DIR = DATA_DIR / "adcheck"

# 抽帧宽度：320 已足够判断「有没有整行文字」，再大只是浪费解码时间
_FRAME_WIDTH = 320
_FRAME_QUALITY = "3"

# 文字条判据参数（基于真实样本标定）
_EDGE_THRESHOLD = 34         # 梯度阈值：高于它算边缘（0-255）
_ROW_COVER_RATIO = 0.22      # 一行里边缘像素占行宽多少才算「文字行」
_BLOCK_MIN_H = 0.02          # 文字块高度下限（占画面高）
_BLOCK_MAX_H = 0.22          # 上限：超过它多半是整幅字卡/场景，不算文字条
_BOTTOM_BAND = 0.84          # 底部条带起始比例
_TOP_BAND = 0.10             # 顶部条带结束比例
_HIT_BIG = 0.35              # 单帧「有大字幕条」的得分阈值
_HIT_BOTTOM = 0.60           # 单帧「有底部跑马灯」的得分阈值

# 文件名里的推广域名特征
_DOMAIN_RE = re.compile(
    r"(?:[a-z0-9][a-z0-9\-]{1,30}\.(?:com|net|org|cc|tv|xyz|top|club|me|info|vip|io))"
    r"|(?:www\.)", re.I)

DEFAULT_CFG: Dict[str, Any] = {
    "enabled": True,
    "head_seconds": 240.0,     # 片头扫描时长（秒）
    "head_interval": 2.0,      # 片头抽帧间隔（秒）
    "tail_seconds": 110.0,     # 片尾扫描时长（秒）
    "tail_interval": 4.0,      # 片尾抽帧间隔（秒）
    "body_frames": 8,          # 片中基准帧数（算比值用，8 帧足够）
    "workers": 4,              # 片中基准段的 seek 并发
    "threshold_likely": 65,    # 分数达到即「高度疑似」
    "threshold_suspect": 35,   # 分数达到即「可疑」
    "keep_sheet": True,        # 保留证据拼版
    "max_sheet_width": 2400,   # 证据图最大宽度（像素）
}


# --------------------------------------------------------------------- 配置
def ad_cfg(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """取广告检测配置段，缺项用 DEFAULT_CFG 兜底（老配置文件没有 adcheck 段）。"""
    raw = (cfg if cfg is not None else load_config()).get("adcheck") or {}
    out = dict(DEFAULT_CFG)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in out and v is not None:
                out[k] = v
    return out


# --------------------------------------------------------------------- 工具
def _ffprobe(ffprobe: str, path: str) -> Dict[str, Any]:
    """读时长 / 分辨率。失败返回 {}。"""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height",
             "-of", "json", path],
            capture_output=True, timeout=45).stdout
        data = json.loads(out.decode("utf-8", "replace") or "{}")
    except Exception:
        return {}
    try:
        dur = float((data.get("format") or {}).get("duration") or 0)
    except Exception:
        dur = 0.0
    vw = vh = 0
    for s in (data.get("streams") or []):
        if s.get("codec_type") == "video":
            vw = int(s.get("width") or 0)
            vh = int(s.get("height") or 0)
            break
    if dur <= 0:
        return {}
    return {"duration": dur, "width": vw, "height": vh}


def _grab(ffmpeg: str, video: str, t: float, outp: str, width: int = _FRAME_WIDTH,
          timeout: int = 60, nokey: bool = True) -> bool:
    """抽单帧（input seeking）。nokey=True 时只解关键帧并放弃精确定位，
    单帧成本从「解一个 GOP」降到「解一个关键帧」（4K 0.7s → 0.1s）。"""
    cmd = [ffmpeg, "-loglevel", "error", "-y"]
    if nokey:
        cmd += ["-skip_frame", "nokey", "-noaccurate_seek"]
    cmd += ["-ss", "%.2f" % t, "-i", video, "-frames:v", "1",
            "-vf", "scale=%d:-2" % width, "-q:v", _FRAME_QUALITY, outp]
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception:
        return False
    return os.path.exists(outp) and os.path.getsize(outp) >= 256


def _row_profile(img) -> Tuple[List[float], int]:
    """缩到 200 宽求边缘，把「每行边缘像素占行宽的比例」压成一维。

    用 resize((1, h)) + BOX 求行均值代替逐像素 Python 循环：单帧 2 万像素的
    循环约 8ms，这里约 0.1ms；整批 80 帧少花近 0.7 秒。
    """
    from PIL import Image, ImageChops, ImageFilter

    g = img.convert("L")
    w, h = g.size
    nh = max(24, int(h * 200 / w))
    g = g.resize((200, nh), Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(0.6))
    ew, eh = g.size
    dx = ImageChops.difference(g.crop((0, 0, ew - 1, eh)),
                               g.crop((1, 0, ew, eh))).crop((0, 0, ew - 1, eh - 1))
    dy = ImageChops.difference(g.crop((0, 0, ew, eh - 1)),
                               g.crop((0, 1, ew, eh))).crop((0, 0, ew - 1, eh - 1))
    e = ImageChops.lighter(dx, dy).point(lambda v: 255 if v >= _EDGE_THRESHOLD else 0)
    hh = e.size[1]
    col = e.resize((1, hh), Image.Resampling.BOX)
    return [v / 255.0 for v in col.tobytes()], hh


def text_scores(img) -> Tuple[float, float, float]:
    """返回 (bigtext, bottom, top)：0~1，越大越像「横贯画面的文字条」。

    bigtext：画面任意位置出现 2%~22% 高的连续文字行块（大字幕 / 说明条）
    bottom ：底部 16% 条带的文字密度（跑马灯 / 网站名）
    top    ：顶部 10% 条带的文字密度（角标 / 台标文字）
    """
    cov, hh = _row_profile(img)
    thr = _ROW_COVER_RATIO

    best = 0.0
    run = 0
    for y in range(hh + 1):
        if y < hh and cov[y] > thr:
            run += 1
            continue
        if run >= 2:
            y0, y1 = y - run, y
            ratio = (y1 - y0) / float(hh)
            if _BLOCK_MIN_H <= ratio <= _BLOCK_MAX_H:
                cover = sum(cov[y0:y1]) / float(y1 - y0)
                best = max(best, min(1.0, cover * 1.6))
        run = 0

    def band(y0: int, y1: int) -> float:
        seg = cov[y0:y1]
        if not seg:
            return 0.0
        hot = sum(1 for v in seg if v > thr * 0.8)
        return min(1.0, (hot / float(len(seg))) * 1.2
                   + (sum(seg) / float(len(seg))) * 1.2)

    return (round(best, 4),
            round(band(int(hh * _BOTTOM_BAND), hh), 4),
            round(band(0, max(2, int(hh * _TOP_BAND))), 4))


def _is_hit(f: Dict[str, Any]) -> bool:
    return f.get("big", 0) > _HIT_BIG or f.get("bottom", 0) > _HIT_BOTTOM


# --------------------------------------------------------------- 抽帧 + 打分
def _seq_frames(ffmpeg: str, video: str, ss: float, span: float, step: float,
                tmpdir: str, tag: str, width: int = _FRAME_WIDTH,
                timeout: int = 900) -> List[Dict[str, Any]]:
    """单进程顺序抽帧：只解码关键帧，fps 滤镜按 step 秒取一张。

    相对「每帧一个 ffmpeg 进程去 seek」，进程开销与解码量都降一个数量级
    （实测 4K 片头 240 秒：45s → 4s；1080p：14s → 2s）。
    input seek 会把 PTS 归零，所以用 showinfo 的 pts_time 加回起点。
    """
    pattern = os.path.join(tmpdir, "%s_%%05d.jpg" % tag)
    cmd = [ffmpeg, "-loglevel", "info", "-nostats", "-y",
           "-skip_frame", "nokey",
           "-ss", "%.3f" % max(0.0, ss), "-t", "%.3f" % span, "-i", video,
           "-vf", "fps=1/%.4f,scale=%d:-2,showinfo" % (step, width),
           "-q:v", _FRAME_QUALITY, pattern]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception:
        return []
    err = proc.stderr.decode("utf-8", "replace")
    pts = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", err)]
    try:
        names = sorted(n for n in os.listdir(tmpdir)
                       if n.startswith(tag + "_") and n.endswith(".jpg"))
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for i, name in enumerate(names):
        p = os.path.join(tmpdir, name)
        try:
            if os.path.getsize(p) < 256:
                continue
        except OSError:
            continue
        out.append({"t": round(ss + (pts[i] if i < len(pts) else i * step), 2),
                    "file": p})
    return out


def _collect_frames(ffmpeg: str, video: str, dur: float, acfg: Dict[str, Any],
                    tmpdir: str,
                    progress: Optional[Callable[[int, int], None]] = None
                    ) -> List[Dict[str, Any]]:
    """三段并行抽帧并打分，返回 [{t, seg, big, bottom, top, file}]。

    片头 / 片尾 —— 单进程顺序抽帧（只解关键帧）：这两个区段本来就连续，
                     顺序解一次远快于几十次独立 seek。
    片中基准段 —— 跨度上千秒，顺序扫描反而更贵，改用少量精确 seek 采样。
    """
    from concurrent.futures import ThreadPoolExecutor

    width = _FRAME_WIDTH
    head_end = min(float(acfg["head_seconds"]), max(30.0, dur * 0.45))
    hstep = max(2.0, float(acfg["head_interval"]))
    body_n = max(4, int(acfg["body_frames"]))
    tstep = max(3.0, float(acfg["tail_interval"]))
    tlen = min(float(acfg["tail_seconds"]), dur * 0.3)
    tail_ss = max(0.0, dur - tlen)
    body_lo, body_hi = dur * 0.30, dur * 0.70
    body_ts = [round(body_lo + (body_hi - body_lo) * (i + 0.5) / body_n, 2)
               for i in range(body_n)]

    total_hint = [max(1, int(head_end / hstep) + body_n + int(tlen / tstep))]
    done = [0]

    def _tick() -> None:
        done[0] += 1
        if progress:
            progress(min(done[0], total_hint[0]), total_hint[0])

    def do_head() -> List[Dict[str, Any]]:
        rows = _seq_frames(ffmpeg, video, 0.0, head_end, hstep, tmpdir, "head", width)
        _tick()
        return rows

    def do_tail() -> List[Dict[str, Any]]:
        rows = _seq_frames(ffmpeg, video, tail_ss, tlen, tstep, tmpdir, "tail", width)
        _tick()
        return rows

    def do_body() -> List[Dict[str, Any]]:
        def one(t: float) -> Optional[Dict[str, Any]]:
            outp = os.path.join(tmpdir, "body_%07.2f.jpg" % t)
            if not _grab(ffmpeg, video, t, outp, width=width):
                return None
            return {"t": t, "file": outp}

        workers = max(1, min(6, int(acfg.get("workers") or 4)))
        with ThreadPoolExecutor(max_workers=min(workers, max(1, body_n))) as ex:
            rows = list(ex.map(one, body_ts))
        _tick()
        return [r for r in rows if r]

    collected: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {"head": ex.submit(do_head), "body": ex.submit(do_body),
                "tail": ex.submit(do_tail)}
        for tag, fut in futs.items():
            try:
                for item in fut.result():
                    item["seg"] = tag
                    collected.append(item)
            except Exception:                   # 单段失败不影响其余两段
                pass

    frames: List[Dict[str, Any]] = []
    for item in collected:
        try:
            from PIL import Image
            with Image.open(item["file"]) as im:
                big, bottom, top = text_scores(im.convert("RGB"))
        except Exception:
            continue
        frames.append({"t": item["t"], "seg": item["seg"], "big": big,
                       "bottom": bottom, "top": top, "file": item["file"]})
    # 片头 → 片中 → 片尾，段内按时间：证据图与结论都按这个顺序读
    order = {"head": 0, "body": 1, "tail": 2}
    frames.sort(key=lambda f: (order.get(f["seg"], 9), f["t"]))
    return frames


# ------------------------------------------------------------------- 判定
def _score(frames: List[Dict[str, Any]], acfg: Dict[str, Any],
           filename: str = "", duration: float = 0.0,
           runtime_sec: float = 0.0) -> Dict[str, Any]:
    """把帧打分聚合成「广告嫌疑」分数（0~100）与可读理由。"""
    head = [f for f in frames if f["seg"] == "head"]
    body = [f for f in frames if f["seg"] == "body"]
    tail = [f for f in frames if f["seg"] == "tail"]

    def hit_ratio(seg: List[Dict[str, Any]]) -> float:
        if not seg:
            return 0.0
        return sum(1 for f in seg if _is_hit(f)) / float(len(seg))

    head_hi = hit_ratio(head)
    body_hi = hit_ratio(body)
    tail_hi = hit_ratio(tail)
    base = max(body_hi, 0.10)          # 基准不能为 0，避免除爆
    head_ratio = head_hi / base
    tail_ratio = tail_hi / base

    score = 0.0
    reasons: List[str] = []
    tags: List[str] = []

    # 1) 片头独立节目：既要「片头文字多」，又要「比片中明显多」——后者才能排除
    #    「整片带剧情字幕」这种与广告无关的情况。
    if head_hi >= 0.18 and head_ratio >= 2.0:
        score += min(60.0, head_hi * 120.0)
        tags.append("intro")
        if head_ratio >= 4.0:
            score += 12.0
        if head_ratio >= 8.0:
            score += 8.0
        reasons.append("片头有 %d%% 的抽帧带整行文字，片中基准仅 %d%%"
                       % (round(head_hi * 100), round(body_hi * 100)))
    elif head_hi >= 0.35:
        # 片头文字很多但片中同样多 → 更像整片带字幕，记低分提示，不当作片头广告
        score += 18.0
        tags.append("headtext")
        reasons.append("片头 %d%% 的抽帧带文字，但片中也有 %d%%，更像整片字幕而非片头广告"
                       % (round(head_hi * 100), round(body_hi * 100)))

    # 2) 片尾独立节目
    if tail_hi >= 0.25:
        score += min(30.0, tail_hi * 40.0)
        tags.append("outro")
        if tail_ratio >= 3.0:
            score += 10.0
        reasons.append("片尾有 %d%% 的抽帧带整行文字" % round(tail_hi * 100))

    # 3) 整片都有文字条（跑马灯 / 全片水印条）
    if body_hi >= 0.50:
        score += 30.0
        tags.append("overlay")
        reasons.append("片中基准段也有 %d%% 的抽帧带文字条，疑整片带广告条"
                       % round(body_hi * 100))

    # 4) 文件名推广域名（弱信号：搬运组前缀很常见，不单独定罪）
    if filename and _DOMAIN_RE.search(filename):
        score += 8.0
        tags.append("domain")
        reasons.append("文件名含推广域名")

    # 5) 时长偏差（弱信号）
    if runtime_sec > 0 and duration > 0:
        gap = (duration - runtime_sec) / runtime_sec
        if gap > 0.20:
            score += 10.0
            tags.append("duration")
            reasons.append("实际 %d 分钟，比标注的 %d 分钟多 %d 分钟"
                           % (round(duration / 60), round(runtime_sec / 60),
                              round(duration / 60) - round(runtime_sec / 60)))

    score = min(100.0, round(score, 1))
    likely = float(acfg["threshold_likely"])
    suspect = float(acfg["threshold_suspect"])
    level = "likely" if score >= likely else ("suspect" if score >= suspect else "clean")

    # 片头广告结束点：从片头段里找**第一段连续 ≥2 帧命中**的位置（片头常见的
    # 黑场 / 空镜 / 厂商 logo 不算广告，允许任意长的未命中前缀），然后向后延伸，
    # 连续 2 帧未命中即视为广告结束。取「最后一个命中帧 + 1 个间隔」。
    step = float(acfg["head_interval"])
    intro_end = 0.0
    hits = [_is_hit(f) for f in head]
    start = -1
    for i in range(len(hits) - 1):
        if hits[i] and hits[i + 1]:
            start = i
            break
    if start >= 0:
        intro_end = head[start]["t"] + step
        gap = 0
        for j in range(start, len(head)):
            if _is_hit(head[j]):
                intro_end = head[j]["t"] + step
                gap = 0
            else:
                gap += 1
                if gap >= 2:
                    break
    # 片尾广告起点：反向找第一段连续 ≥2 帧命中（连续 2 帧未命中即停）
    outro_start = 0.0
    thits = [_is_hit(f) for f in tail]
    tail_start = -1
    for j in range(len(thits) - 1, 0, -1):
        if thits[j] and thits[j - 1]:
            tail_start = j
            break
    if tail_start >= 0:
        outro_start = tail[tail_start]["t"]
        gap = 0
        for k in range(tail_start, -1, -1):
            if _is_hit(tail[k]):
                outro_start = tail[k]["t"]
                gap = 0
            else:
                gap += 1
                if gap >= 2:
                    break

    return {
        "score": score, "level": level, "tags": tags, "reasons": reasons,
        "head_hi": round(head_hi, 3), "body_hi": round(body_hi, 3),
        "tail_hi": round(tail_hi, 3),
        "frames_head": len(head), "frames_body": len(body), "frames_tail": len(tail),
        "intro_end": round(intro_end, 1),
        "outro_start": round(outro_start, 1) if outro_start else 0.0,
    }


# --------------------------------------------------------------- 证据拼版
def build_sheet(frames: List[Dict[str, Any]], outp: str,
                max_width: int = 2400) -> Optional[str]:
    """把抽帧拼成缩略图墙（带时间戳、可疑帧红框），片头与片尾分段排列。"""
    from PIL import Image, ImageDraw

    head = [f for f in frames
            if f["seg"] == "head" and os.path.exists(f.get("file") or "")]
    tail = [f for f in frames
            if f["seg"] == "tail" and os.path.exists(f.get("file") or "")]
    if not head and not tail:
        return None
    # 片头抽帧已加密到 2 秒一张（120 帧），证据图隔帧取样，控制成图规模；
    # 打分用的是全部帧，这里只影响可视化的疏密。
    if len(head) > 72:
        head = head[::2]
    try:
        with Image.open((head or tail)[0]["file"]) as probe:
            ar = probe.size[1] / float(max(1, probe.size[0]))
    except Exception:
        ar = 9 / 16.0

    cols = 12
    tw = max(120, min(300, int(max_width / cols)))
    th = max(60, int(tw * ar))
    font = None
    try:
        from PIL import ImageFont
        font = ImageFont.load_default(size=max(12, min(18, tw // 16)))
    except Exception:
        font = None
    cap = 20 if font is None else max(16, min(24, tw // 14))   # 时间戳行高
    row_h = th + cap
    rows_head = (len(head) + cols - 1) // cols
    rows_tail = (len(tail) + cols - 1) // cols
    rows_total = rows_head + rows_tail + (1 if rows_head and rows_tail else 0)
    pad = 8
    height = pad * 2 + max(1, rows_total) * row_h
    width = cols * tw + pad * 2
    sheet = Image.new("RGB", (width, height), (18, 18, 18))
    draw = ImageDraw.Draw(sheet)

    def place(items: List[Dict[str, Any]], row0: int) -> int:
        if not items:
            return row0
        for i, f in enumerate(items):
            try:
                with Image.open(f["file"]) as im:
                    tile = im.convert("RGB").resize((tw, th))
            except Exception:
                continue
            x = pad + (i % cols) * tw
            y = pad + (row0 + i // cols) * row_h
            sheet.paste(tile, (x, y))
            hit = _is_hit(f)
            outline = (235, 60, 60) if hit else (50, 50, 50)
            draw.rectangle([x, y, x + tw - 1, y + th - 1], outline=outline)
            if hit:                                  # 可疑帧加粗边框，缩略图下也看得见
                draw.rectangle([x + 1, y + 1, x + tw - 2, y + th - 2], outline=outline)
            draw.text((x + 3, y + th + 1), "%ds" % round(f["t"]),
                      fill=(255, 90, 90) if hit else (120, 200, 120), font=font)
        return row0 + (len(items) + cols - 1) // cols

    row = place(head, 0)
    if tail:
        row += 1 if head else 0                    # 空一行区分片头 / 片尾
        row = place(tail, row)

    os.makedirs(os.path.dirname(outp) or ".", exist_ok=True)
    sheet.save(outp, quality=82, optimize=True)
    return outp


# ------------------------------------------------------------------- 主入口
def analyze_file(path: str, cfg: Optional[Dict[str, Any]] = None,
                 runtime_sec: float = 0.0, filename: str = "",
                 movie_id: int = 0,
                 progress: Optional[Callable[[int, int], None]] = None
                 ) -> Dict[str, Any]:
    """检测单个视频文件。返回结果字典（含分数、信号、证据图文件名）。

    ffmpeg 不可用 / 文件读不了 → 返回 {"ok": False, "error": ...}，不抛异常。
    """
    from .ext_components import ffmpeg_exe, ffprobe_exe

    acfg = ad_cfg(cfg)
    ffmpeg = ffmpeg_exe(cfg)
    ffprobe = ffprobe_exe(cfg)
    info = _ffprobe(ffprobe, path)
    if not info:
        return {"ok": False, "error": "无法读取视频信息（ffprobe 失败或文件损坏）",
                "path": path, "movie_id": movie_id}

    dur = float(info["duration"])
    tmpdir = tempfile.mkdtemp(prefix="adcheck_")
    t0 = time.time()
    try:
        frames = _collect_frames(ffmpeg, path, dur, acfg, tmpdir, progress=progress)
        if len(frames) < 6:
            return {"ok": False, "error": "抽帧不足（ffmpeg 不可用或视频无法解码）",
                    "path": path, "movie_id": movie_id}
        sig = _score(frames, acfg, filename=filename or os.path.basename(path),
                     duration=dur, runtime_sec=runtime_sec)
        sheet = ""
        if acfg.get("keep_sheet") and movie_id:
            sheet_path = os.path.join(str(SHEET_DIR), "%d.jpg" % movie_id)
            if build_sheet(frames, sheet_path,
                           max_width=int(acfg.get("max_sheet_width") or 2400)):
                sheet = "%d.jpg" % movie_id
        return {
            "ok": True,
            "movie_id": movie_id,
            "path": path,
            "duration": round(dur, 1),
            "width": info.get("width", 0),
            "height": info.get("height", 0),
            "runtime_sec": round(runtime_sec, 1),
            "score": sig["score"],
            "level": sig["level"],
            "tags": sig["tags"],
            "reasons": sig["reasons"],
            "signals": {
                "head_hi": sig["head_hi"], "body_hi": sig["body_hi"],
                "tail_hi": sig["tail_hi"],
                "frames": {"head": sig["frames_head"], "body": sig["frames_body"],
                           "tail": sig["frames_tail"]},
                "intro_end": sig["intro_end"], "outro_start": sig["outro_start"],
            },
            "sheet": sheet,
            "elapsed": round(time.time() - t0, 1),
        }
    except Exception as e:                      # noqa: BLE001 单部失败不影响整批
        return {"ok": False, "error": str(e)[:200], "path": path, "movie_id": movie_id}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ------------------------------------------------------------------- 落库
def save_result(conn, res: Dict[str, Any], file_id: int = 0) -> None:
    """写入 / 更新 ad_checks（movie_id 唯一，重跑即覆盖）。"""
    if not res.get("ok"):
        return
    conn.execute(
        "INSERT INTO ad_checks (movie_id, file_id, path, score, level, duration, "
        "runtime_sec, head_hi, body_hi, tail_hi, signals, sheet, checked_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime')) "
        "ON CONFLICT(movie_id) DO UPDATE SET "
        "file_id=excluded.file_id, path=excluded.path, score=excluded.score, "
        "level=excluded.level, duration=excluded.duration, runtime_sec=excluded.runtime_sec, "
        "head_hi=excluded.head_hi, body_hi=excluded.body_hi, tail_hi=excluded.tail_hi, "
        "signals=excluded.signals, sheet=excluded.sheet, "
        "checked_at=datetime('now','localtime')",
        (res["movie_id"], file_id, res["path"], res["score"], res["level"],
         res.get("duration") or 0, res.get("runtime_sec") or 0,
         res["signals"]["head_hi"], res["signals"]["body_hi"], res["signals"]["tail_hi"],
         json.dumps({**res["signals"], "tags": res.get("tags") or [],
                     "reasons": res.get("reasons") or []}, ensure_ascii=False),
         res.get("sheet") or ""))
    conn.commit()


def get_result(conn, movie_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM ad_checks WHERE movie_id=?", (movie_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["signals"] = json.loads(d.get("signals") or "{}")
    except Exception:
        d["signals"] = {}
    return d


def list_results(conn, level: str = "", include_ignored: bool = False,
                 limit: int = 300, offset: int = 0) -> List[Dict[str, Any]]:
    """按分数倒序列出检测结果（默认排除人工判定为「不是广告」的）。"""
    where = []
    args: List[Any] = []
    if not include_ignored:
        where.append("a.ignored = 0")
    if level:
        where.append("a.level = ?")
        args.append(level)
    sql = ("SELECT a.*, m.code, m.title, m.cover, m.resolution "
           "FROM ad_checks a LEFT JOIN movies m ON m.id = a.movie_id ")
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY a.score DESC, a.movie_id DESC LIMIT ? OFFSET ?"
    args.extend([max(1, min(2000, int(limit))), max(0, int(offset))])
    rows = []
    for r in conn.execute(sql, args).fetchall():
        d = dict(r)
        try:
            d["signals"] = json.loads(d.get("signals") or "{}")
        except Exception:
            d["signals"] = {}
        rows.append(d)
    return rows


def counts(conn) -> Dict[str, int]:
    """统计各等级数量（用于前端顶部汇总）。"""
    out = {"likely": 0, "suspect": 0, "clean": 0, "ignored": 0, "total": 0}
    for r in conn.execute("SELECT level, ignored, COUNT(*) AS n FROM ad_checks "
                          "GROUP BY level, ignored").fetchall():
        n = int(r["n"])
        out["total"] += n
        if int(r["ignored"] or 0):
            out["ignored"] += n
        elif r["level"] in out:
            out[r["level"]] += n
    return out


def set_ignored(conn, movie_id: int, ignored: bool) -> bool:
    cur = conn.execute("UPDATE ad_checks SET ignored=? WHERE movie_id=?",
                       (1 if ignored else 0, movie_id))
    conn.commit()
    return cur.rowcount > 0


def clear(conn, movie_id: int = 0) -> int:
    """删除检测记录（movie_id=0 表示全清）；同时删除证据图。"""
    if movie_id:
        rows = conn.execute("SELECT movie_id, sheet FROM ad_checks WHERE movie_id=?",
                            (movie_id,)).fetchall()
    else:
        rows = conn.execute("SELECT movie_id, sheet FROM ad_checks").fetchall()
    for r in rows:
        name = (r["sheet"] or "") or ("%d.jpg" % r["movie_id"])
        try:
            p = os.path.join(str(SHEET_DIR), name)
            if os.path.exists(p):
                os.unlink(p)
        except OSError:
            pass
    cur = (conn.execute("DELETE FROM ad_checks WHERE movie_id=?", (movie_id,))
           if movie_id else conn.execute("DELETE FROM ad_checks"))
    conn.commit()
    return cur.rowcount


def sheet_path(movie_id: int) -> Optional[str]:
    """证据图的绝对路径（不存在返回 None）。"""
    p = os.path.join(str(SHEET_DIR), "%d.jpg" % movie_id)
    return p if os.path.exists(p) else None
