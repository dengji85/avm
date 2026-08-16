# -*- coding: utf-8 -*-
"""文件名解析：从文件名中提取番号、清洗标题、识别字幕/无码/分片等标记。

设计要点：
1. 先剥离发布组、站点域名、方括号广告等噪声；
2. 在噪声清洗「之前」提取字幕/无码等标记，避免 -C / -U 这类后缀被误删；
3. 按照「特例规则 -> 通用规则」的顺序匹配番号，先命中先返回；
4. 番号统一格式化（大写字母 + 连字符 + 去前导零后补足 3 位）。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------- 噪声清洗

# 站点/发布组前后缀
_NOISE_PATTERNS = [
    re.compile(r"^\s*[a-z0-9\-]+\.(?:com|net|org|cc|xyz|tv|me|info|app|top|club|life|vip|biz|io|la)\s*[@\-_]+", re.I),
    re.compile(r"(?:https?://)?(?:www\.)?[a-z0-9\-]{2,}\.(?:com|net|org|cc|xyz|tv|me|info|app|top|club|life|vip|biz|io|la)", re.I),
    re.compile(r"@[a-z0-9_\-]{2,}", re.I),
    re.compile(r"\[[^\]]*\]"),
    re.compile(r"【[^】]*】"),
    re.compile(r"（[^）]*）"),
]

# 画质/编码等技术噪声
_TECH_NOISE = re.compile(
    r"\b(?:1080p|1080i|720p|480p|2160p|4k|8k|uhd|fhd|hd|sd|x264|x265|h\.?264|h\.?265|hevc|avc|"
    r"aac|ac3|dts|flac|mp3|web-?dl|webrip|bluray|blu-?ray|bdrip|bdmv|hdrip|dvdrip|dvdiso|remux|"
    r"xvid|divx|10bit|8bit|60fps|30fps|24fps|hdr|sdr|multi|repack|proper)\b",
    re.I,
)

# 标记识别（在清洗前跑）
_FLAG_RULES: Dict[str, re.Pattern] = {
    "subtitle": re.compile(
        r"(中文字幕|中字|简体|繁體|繁体|字幕|chinese\s*sub(?:title)?s?|\bsubbed\b|"
        r"[-_](?:c|ch|uc|chs|cht|sub)(?=[\s.\-_\]]|$)|"      # -C / -CH / _UC 等带分隔符
        r"(?<=\d)(?:ch|chs|cht|chn|chi|chinese|unc|uc|c|u|sub)(?=$|[\s._\-]))",  # 171CH 紧贴数字无分隔符
        re.I,
    ),
    "uncensored": re.compile(
        r"(无码|無碼|無修正|无修正|uncensor(?:ed)?|uncen|无码破解|破解|"
        r"[-_](?:u|uc|cl)(?=[\s.\-_\]]|$)|(?<=\d)(?:unc|uc|cl)(?=$|[\s._\-]))",
        re.I,
    ),
    "leak": re.compile(r"(流出|泄漏|泄露|\bleak(?:ed)?\b)", re.I),
    "hd4k": re.compile(r"(\b4k\b|\b2160p\b|\buhd\b)", re.I),
    "vr": re.compile(r"(\bvr\b|\bvr视频\b|3dvr)", re.I),
}

# 分片：-cd1 / -part2 / _pt3 / 尾部单字母
# 注意：尾部单字母刻意排除 c 和 u，因为 -C / -U / -UC 是「中文字幕 / 无码」的通行写法
_PART_PATTERNS = [
    re.compile(r"[-_\s.]?(?:cd|part|pt|disc|disk|vol)[-_\s.]?(\d{1,2})(?=[\s.\-_\]]|$)", re.I),
    re.compile(r"[-_\s](\d{1,2})of\d{1,2}(?=[\s.\-_\]]|$)", re.I),
    re.compile(r"[-_]([abde])(?=\.[a-z0-9]{2,4}$|$)", re.I),
]

# 清洗后若标题只剩这些残渣，说明并没有真正的标题
_JUNK_TITLE = {
    "c", "u", "uc", "ch", "chs", "cht", "sub", "subs", "full", "hd", "fhd", "new",
    "mp4", "mkv", "avi", "wmv", "final", "ver", "part", "cd", "carib", "leak", "fin",
}

# 通用规则里需要排除的假前缀
_FAKE_PREFIX = {
    "cd", "part", "pt", "disc", "disk", "vol", "ep", "sp", "ch", "no", "nov", "hd",
    "mp", "ts", "av", "vr", "the", "and", "for", "ver", "rev", "top", "new", "sub",
}


def _fmt_num(num: str, min_width: int = 3) -> str:
    stripped = num.lstrip("0") or "0"
    return stripped.zfill(min_width) if len(stripped) < min_width else stripped


# ---------------------------------------------------------------- 番号规则
# 每条规则： (规则名, 正则, 格式化函数)
_CODE_RULES: list[tuple[str, re.Pattern, Any]] = [
    (
        "FC2",
        re.compile(r"fc-?2[\s\-_]*(?:ppv|PPV)?[\s\-_]*(\d{6,8})", re.I),
        lambda m: f"FC2-PPV-{m.group(1)}",
    ),
    (
        "HEYZO",
        re.compile(r"heyzo[\s\-_]*(?:hd)?[\s\-_]*(\d{4})", re.I),
        lambda m: f"HEYZO-{m.group(1)}",
    ),
    (
        "XXX-AV",
        re.compile(r"xxx[\s\-_]*av[\s\-_]*(\d{4,5})", re.I),
        lambda m: f"XXX-AV-{m.group(1)}",
    ),
    (
        "MARKET",  # 259LUXU-1234 / 300MAAN-123 / 200GANA-2000
        re.compile(r"(?<![a-z0-9])(\d{2,4}[a-z]{2,6})[\s\-_]*(\d{2,5})(?![a-z0-9])", re.I),
        lambda m: f"{m.group(1).upper()}-{_fmt_num(m.group(2))}",
    ),
    (
        "DATE-NUM",  # 无码厂常见 123456-789 / 010112_001
        re.compile(r"(?<!\d)(\d{6})[\-_](\d{2,3})(?!\d)"),
        lambda m: f"{m.group(1)}-{m.group(2)}",
    ),
    (
        "TOKYOHOT",  # n1234 / k1234
        re.compile(r"(?<![a-z0-9])(n|k)(\d{4})(?![a-z0-9])", re.I),
        lambda m: f"{m.group(1).lower()}{m.group(2)}",
    ),
    (
        "STANDARD",  # ABC-123 / ABCD00123 / ssis 001 / IRO061C / JUR-171CH / MIRD-181CHSUB
        # 厂牌前缀 [2,6] 字母 + 可选分隔符 + 编号；编号后紧跟的字幕/版本标记
        # （ch/chs/cht/chn/chi/c/u/uc/unc/sub/multi 等，可无分隔符紧贴或带 -_）一并吞掉，
        # 格式化时只保留「前缀-编号」，字幕信息由 detect_flags 单独记录。
        re.compile(
            r"(?<![a-z0-9])"
            r"([a-z]{2,6})"                                              # 厂牌前缀
            r"[\s\-_.]?"                                                 # 可选分隔符
            r"(\d{1,5})"                                                 # 编号（含 1 位，兼容 JUY-1 等低编号厂牌）
            r"(?:[\s._\-]?(?:ch|chs|cht|chn|chi|chinese|unc|uc|c|u|cl|sub|multi))*"  # 尾随版本/字幕标记
            r"(?![a-z0-9])",
            re.I,
        ),
        lambda m: f"{m.group(1).upper()}-{_fmt_num(m.group(2))}",
    ),
]


def _strip_noise(text: str) -> str:
    out = text
    # 剥离紧贴番号数字后的字幕后缀：760CH -> 760 / 123unc -> 123
    # 仅作用于「数字后」避免误伤字母前缀番号（如 CHN-123 的 CHN 不在数字后）
    out = re.sub(r"(?<=\d)(?:ch|chs|cht|chinese|unc|uc|c|sub)(?=$|[\s._\-])", " ", out, flags=re.I)
    for pat in _NOISE_PATTERNS:
        out = pat.sub(" ", out)
    out = _TECH_NOISE.sub(" ", out)
    out = re.sub(r"\s+", " ", out).strip(" .-_")
    return out


def detect_flags(raw: str) -> Dict[str, int]:
    return {name: int(bool(pat.search(raw))) for name, pat in _FLAG_RULES.items()}


_RES_RULES = [
    (re.compile(r"\b(?:2160p|4k|uhd)\b", re.I), "2160p"),
    (re.compile(r"\b(?:1440p|2k)\b", re.I), "1440p"),
    (re.compile(r"\b(?:1080p|fullhd|fhd)\b", re.I), "1080p"),
    (re.compile(r"\b(?:720p)\b", re.I), "720p"),
    (re.compile(r"\b(?:480p|sd)\b", re.I), "480p"),
]


def detect_resolution(raw: str) -> str:
    """从文件名/目录名提取分辨率标签，用于同番号多版本的质量比较。"""
    for pat, label in _RES_RULES:
        if pat.findall(raw):
            return label
    return ""


def detect_part(stem: str) -> int:
    for idx, pat in enumerate(_PART_PATTERNS):
        m = pat.search(stem)
        if not m:
            continue
        val = m.group(1)
        if val.isdigit():
            n = int(val)
            if 1 <= n <= 20:
                return n
        else:
            return ord(val.lower()) - ord("a") + 1
    return 1


def extract_code(text: str) -> tuple[Optional[str], str, str]:
    """返回 (格式化番号, 原始匹配串, 规则名)。未识别时返回 (None, '', '')。"""
    cleaned = _strip_noise(text)
    for name, pat, fmt in _CODE_RULES:
        for m in pat.finditer(cleaned):
            if name == "STANDARD" and m.group(1).lower() in _FAKE_PREFIX:
                continue
            if name == "MARKET" and len(m.group(1)) < 4:
                continue
            return fmt(m), m.group(0), name
    return None, "", ""


def _clean_title(stem: str, code_raw: str) -> str:
    title = _strip_noise(stem)
    if code_raw:
        title = title.replace(code_raw, " ")
    title = re.sub(
        r"(中文字幕|中字|简体|繁體|繁体|字幕|无码|無碼|无修正|無修正|uncensored|uncen|"
        r"chinese\s*sub(?:title)?s?|subbed|流出|泄漏|泄露|leaked|leak|破解)",
        " ", title, flags=re.I,
    )
    title = re.sub(r"[-_\s.]+(?:cd|part|pt|disc|disk|vol)[-_\s.]?\d{1,2}\b", " ", title, flags=re.I)
    # 去掉孤立的 -c / -u / -uc / -ch 之类的版本后缀
    title = re.sub(r"(?:^|[\s._\-])(?:c|u|uc|ch|chs|cht|sub)(?=[\s._\-]|$)", " ", title, flags=re.I)
    title = re.sub(r"[\s._\-]+", " ", title).strip(" .-_")
    if title.lower() in _JUNK_TITLE or len(title) <= 1:
        return ""
    return title


def path_fingerprint(path: str) -> str:
    return hashlib.sha1(str(path).lower().encode("utf-8", "ignore")).hexdigest()[:12]


def parse_file(path: str | Path) -> Dict[str, Any]:
    """解析单个视频文件路径，返回归并所需的全部信息。"""
    p = Path(path)
    stem = p.stem
    raw = f"{p.parent.name} {stem}"  # 目录名常常也带番号，一并参与判断

    flags = detect_flags(raw)
    part = detect_part(stem)
    resolution = detect_resolution(raw)

    code, code_raw, rule = extract_code(stem)
    if not code:  # 文件名没有就退回目录名
        code, code_raw, rule = extract_code(p.parent.name)
        if code:
            rule += "(dir)"

    title = _clean_title(stem, code_raw)
    if not title:
        title = code or stem

    if code:
        key = code.upper()
        has_code = 1
    else:
        key = f"NC:{path_fingerprint(str(p))}"
        has_code = 0

    return {
        "key": key,
        "code": code or "",
        "has_code": has_code,
        "code_rule": rule,
        "title": title,
        "part": part,
        "resolution": resolution,
        "folder": str(p.parent),
        "filename": p.name,
        "ext": p.suffix.lower(),
        **flags,
    }
