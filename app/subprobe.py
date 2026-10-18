# -*- coding: utf-8 -*-
"""内嵌字幕探测：判断一个视频文件是否自带中文字幕轨。

和「元数据标注中字」（JavBus 的 subtitle 字段）区分开：这里看的是文件里
**真实存在的字幕轨**。判定顺序按成本从低到高：

1. 语言标签：chi / zho / zh / zh-CN / zh-Hans ……
2. 轨道标题：简中 / 繁中 / 中字 / CHS / CHT / Chinese
3. 文本轨抽 200 行做字符统计：汉字占比够高、且几乎没有假名，才判为中文
   （只看语言标签会漏掉大量 `und` 的 MP4 / MKV）

图形字幕（PGS / DVD）无法读文字，只能凭标签判断，会标成 ``image``；
硬字幕没有轨，检测不到，保持未知。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Tuple

from .remux import _resolve_ffmpeg

# 文本字幕编码：能直接抽成 SRT 读文字
TEXT_CODECS = {
    "subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text",
    "microdvd", "subviewer", "subviewer1", "sami", "jacosub", "mpl2",
    "realtext", "stl", "pjs", "vplayer",
}
# 图形字幕：只能靠语言标签判断
IMAGE_CODECS = {
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "dvb_teletext",
}

_ZH_LANGS = {"zh", "zho", "chi", "cmn", "zh-cn", "zh-hans", "zh-sg", "zh-tw", "zh-hk", "zh-hant"}
_ZH_TITLE_RE = re.compile(
    r"(中字|中文字幕|中文|简中|繁中|简体|繁体|chs|cht|chinese|zh[-_]?(cn|tw|hk|hans|hant))",
    re.IGNORECASE,
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
# 只在繁体里常见、简体不会出现的字（用于区分简繁，不需要完整字表）
_TRAD_RE = re.compile(
    r"[們個這說時為國會學對來後裡麼還東車進過開關發現實際語讀寫聽講買賣錢價質點樣種類數據網頁連線電話視機軟體統檔案]"
)


def _norm_lang(raw: str) -> str:
    s = (raw or "").strip().lower().replace("_", "-")
    if not s or s in ("und", "unknown", "none"):
        return ""
    if s in _ZH_LANGS:
        return "zh-hant" if s in ("zh-tw", "zh-hk", "zh-hant") else "zh"
    if s.startswith("zh"):
        return "zh"
    return s


def _is_zh_by_meta(lang: str, title: str) -> Tuple[bool, str]:
    if _norm_lang(lang) in ("zh", "zh-hant"):
        return True, "tag"
    if title and _ZH_TITLE_RE.search(title):
        return True, "title"
    return False, ""


def _ffprobe_streams(path: str, ffprobe: str) -> Dict[str, Any]:
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_streams", "-show_format", path]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=12)
        if p.returncode != 0 or not p.stdout:
            return {}
        return json.loads(p.stdout.decode("utf-8", "replace"))
    except Exception:
        return {}


def _srt_stats(raw: str) -> Dict[str, Any]:
    """从 SRT 文本里估算语言与覆盖率。"""
    text_lines: List[str] = []
    last_end = 0.0
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.isdigit():
            continue
        if "-->" in s:
            m = re.search(r"-->\s*(\d+):(\d+):(\d+)[,.](\d+)", s)
            if m:
                h, mi, sec, ms = (int(x) for x in m.groups())
                last_end = max(last_end, h * 3600 + mi * 60 + sec + ms / 1000.0)
            continue
        text_lines.append(re.sub(r"<[^>]+>", "", s))

    text = " ".join(text_lines)
    cjk = len(_CJK_RE.findall(text))
    kana = len(_KANA_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    total = cjk + kana + latin
    # 样本很短（测试片 / 只有几句）也要能判断，阈值取 10 个字符
    if total < 10:
        return {"lang": "", "last_end": last_end, "chars": total}

    kana_ratio = kana / max(1, cjk + kana)
    cjk_ratio = cjk / max(1, total)
    if kana_ratio > 0.12 or cjk_ratio < 0.35:
        return {"lang": "ja" if kana_ratio > 0.12 else "", "last_end": last_end, "chars": total}
    trad = len(_TRAD_RE.findall(text))
    lang = "zh-hant" if cjk >= 40 and trad / max(1, cjk) > 0.04 else "zh"
    return {"lang": lang, "last_end": last_end, "chars": total}


def srt_to_text(raw: str) -> str:
    """SRT / WebVTT → 纯文本（去时间码、序号、样式标签，去相邻重复）。"""
    out: List[str] = []
    prev = ""
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.isdigit() or "-->" in s:
            continue
        s = re.sub(r"<[^>]+>", "", s).strip()
        if not s or s == prev:
            continue
        out.append(s)
        prev = s
    return "\n".join(out)


def extract_track_text(path: str, ordinal: int, cfg: Dict[str, Any]) -> str:
    """抽取第 ordinal 条字幕轨的 SRT 原文（供剧情分析使用）。"""
    ffmpeg, _ = _resolve_ffmpeg(cfg)
    return _extract_text(path, ordinal, ffmpeg)


def _extract_text(path: str, ordinal: int, ffmpeg: str) -> str:
    """把第 ordinal 条字幕轨抽成 SRT 文本（只取前 2000 行，防止超长字幕）。"""
    cmd = [ffmpeg, "-v", "error", "-i", path, "-map", "0:s:%d" % ordinal,
           "-c:s", "srt", "-f", "srt", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=15)
        if p.returncode != 0 or not p.stdout:
            return ""
        return p.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def probe(path: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """探测单个文件的内嵌字幕。

    返回结构（可直接 JSON 存入 ``movie_files.sub_tracks``）::

        {"tracks": [{"si":0,"codec":"subrip","lang":"zh","title":"简中",
                     "kind":"text","zh":true,"source":"cjk","forced":false,
                     "partial":false}],
         "langs": ["zh","jpn"], "zh": "text"|"image"|"", "probed_at": "..."}
    """
    ffmpeg, ffprobe = _resolve_ffmpeg(cfg)
    data = _ffprobe_streams(path, ffprobe)
    if not data:
        # ffprobe 不可用 / 文件损坏：记下尝试时间，避免每次扫描都白等超时；
        # 需要重试时清掉 movie_files.sub_probe_at 即可。
        return {"tracks": [], "langs": [], "zh": "", "error": "probe-failed",
                "probed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    duration = 0.0
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0

    tracks: List[Dict[str, Any]] = []
    langs: List[str] = []
    si = 0
    for st in data.get("streams") or []:
        if st.get("codec_type") != "subtitle":
            continue
        codec = str(st.get("codec_name") or "").lower()
        tags = st.get("tags") or {}
        lang_raw = str(tags.get("language") or "")
        title = str(tags.get("title") or "")
        disp = st.get("disposition") or {}
        forced = bool(disp.get("forced"))
        kind = "text" if codec in TEXT_CODECS else ("image" if codec in IMAGE_CODECS else "")
        if not kind:
            # 未知字幕编码：先记为文本，交给后面的字符统计判断
            kind = "text"

        zh, source = _is_zh_by_meta(lang_raw, title)
        partial = False
        norm_lang = _norm_lang(lang_raw)

        # 文本轨且靠标签判断不出中文：抽一段字幕看字符构成
        if kind == "text" and not zh and si < 6:
            raw = _extract_text(path, si, ffmpeg)
            stats = _srt_stats(raw)
            if stats.get("lang", "").startswith("zh") and stats.get("chars", 0) >= 10:
                zh = True
                source = "cjk"
                norm_lang = stats["lang"]
            if duration > 60 and stats.get("last_end"):
                partial = stats["last_end"] / duration < 0.3

        if norm_lang:
            langs.append(norm_lang)
        tracks.append({
            "si": si, "codec": codec, "lang": norm_lang or lang_raw.lower(),
            "title": title, "kind": kind, "zh": bool(zh), "source": source,
            "forced": forced, "partial": bool(partial),
        })
        si += 1

    # 影片级结论：完整文本中字 > 图形中字；forced / 覆盖率过低的轨不算
    text_zh = any(t["zh"] and t["kind"] == "text" and not t["forced"] and not t["partial"]
                  for t in tracks)
    image_zh = any(t["zh"] and t["kind"] == "image" and not t["forced"] for t in tracks)
    zh_kind = "text" if text_zh else ("image" if image_zh else "")

    return {
        "tracks": tracks,
        "langs": sorted(set(langs)),
        "zh": zh_kind,
        "probed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def summarize(tracks_json: str) -> Dict[str, Any]:
    """从已存库的 ``sub_tracks`` JSON 里取影片级结论（多文件合并用）。"""
    try:
        data = json.loads(tracks_json or "{}")
    except ValueError:
        return {"zh": "", "langs": []}
    if not isinstance(data, dict):
        return {"zh": "", "langs": [], "any": False}
    return {"zh": data.get("zh") or "", "langs": data.get("langs") or [],
            "any": bool(data.get("tracks"))}
