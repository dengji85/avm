# -*- coding: utf-8 -*-
"""Kodi / Emby 风格 NFO 文件的读写。"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.dom import minidom


def _text(node: Optional[ET.Element]) -> str:
    return (node.text or "").strip() if node is not None else ""


def _first(root: ET.Element, *paths: str) -> str:
    for p in paths:
        v = _text(root.find(p))
        if v:
            return v
    return ""


def parse_nfo(path: str | Path) -> Optional[Dict[str, Any]]:
    """读取 .nfo(XML) 或 .json 元数据文件。"""
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    if not raw.strip():
        return None

    if p.suffix.lower() == ".json":
        try:
            data = json.loads(raw)
        except Exception:
            return None
        return _normalize_json(data)

    # 兼容 BOM / XML 声明前的杂质
    raw = raw[raw.find("<"):] if "<" in raw else raw
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None

    actresses: List[str] = []
    for actor in root.findall("actor"):
        name = _text(actor.find("name"))
        if name:
            actresses.append(name)

    genres = [_text(g) for g in root.findall("genre") if _text(g)]
    tags = [_text(t) for t in root.findall("tag") if _text(t)]

    release = _first(root, "premiered", "releasedate", "release_date", "dateadded")
    year = _first(root, "year")
    if not release and re.fullmatch(r"\d{4}", year or ""):
        release = f"{year}-01-01"

    runtime_raw = _first(root, "runtime", "durationinseconds", "fileinfo/streamdetails/video/durationinseconds")
    runtime = 0
    m = re.search(r"\d+", runtime_raw or "")
    if m:
        runtime = int(m.group(0))
        if runtime > 1000:  # 秒 -> 分钟
            runtime //= 60

    rating = 0.0
    rating_raw = _first(root, "rating", "ratings/rating/value", "userrating")
    try:
        rating = float(rating_raw)
    except (TypeError, ValueError):
        rating = 0.0

    return _clean({
        "code": _first(root, "num", "id", "uniqueid"),
        "title": _first(root, "title", "localtitle"),
        "original_title": _first(root, "originaltitle"),
        "plot": _first(root, "plot", "outline"),
        "release_date": release,
        "runtime": runtime,
        "studio": _first(root, "studio", "maker"),
        "publisher": _first(root, "label", "publisher"),
        "series": _first(root, "set/name", "set", "series"),
        "director": _first(root, "director"),
        "rating": rating,
        "cover": _first(root, "art/poster", "poster", "thumb", "cover"),
        "fanart": _first(root, "art/fanart", "fanart/thumb"),
        "actresses": actresses,
        "genres": genres,
        "tags": tags,
    })


_JSON_ALIASES = {
    "title": ["title", "name", "movie_title"],
    "original_title": ["original_title", "originaltitle", "jp_title"],
    "plot": ["plot", "outline", "description", "summary"],
    "release_date": ["release_date", "premiered", "release", "date"],
    "runtime": ["runtime", "duration", "length"],
    "studio": ["studio", "maker", "producer"],
    "publisher": ["publisher", "label"],
    "series": ["series", "set"],
    "director": ["director"],
    "rating": ["rating", "score"],
    "cover": ["cover", "poster", "thumb", "image", "cover_url"],
    "fanart": ["fanart", "backdrop", "big_cover"],
    "actresses": ["actresses", "actress", "actors", "stars", "performers"],
    "genres": ["genres", "genre", "categories", "tags"],
}


def _normalize_json(data: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    out: Dict[str, Any] = {}
    lower = {str(k).lower(): v for k, v in data.items()}
    for field, aliases in _JSON_ALIASES.items():
        for a in aliases:
            if a in lower and lower[a] not in (None, ""):
                out[field] = lower[a]
                break
    return _clean(out)


def _clean(meta: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("actresses", "genres", "tags"):
        val = meta.get(key)
        if isinstance(val, str):
            meta[key] = [x.strip() for x in re.split(r"[,，、;/|]", val) if x.strip()]
        elif isinstance(val, list):
            names = []
            for item in val:
                if isinstance(item, dict):
                    n = item.get("name") or item.get("title") or ""
                else:
                    n = str(item)
                if str(n).strip():
                    names.append(str(n).strip())
            meta[key] = names
        else:
            meta[key] = []
    return {k: v for k, v in meta.items() if v not in (None, "", [], 0) or k in ("runtime", "rating")}


def _norm_name(name: str) -> str:
    """规范化文件名用于匹配：主名与扩展名分别去首尾空白后再拼接。

    手动整理的文件常在扩展名前多一个空格（如 "812MMC-004 .nfo"、
    "812MMC-004 .mp4"），直接比对会因这个空格永远匹配不上。
    """
    p = Path(name)
    return (p.stem.strip() + p.suffix.strip()).lower()


def find_sidecar(video_path: str | Path, code: str = "") -> Optional[Path]:
    """查找视频旁边的元数据文件。"""
    p = Path(video_path)
    folder = p.parent
    if not folder.is_dir():
        return None
    bases = [p.stem, "movie", "index"]
    if code:
        bases.insert(1, code)
    try:
        existing: Dict[str, Path] = {}
        for f in folder.iterdir():
            if f.is_file():
                existing.setdefault(_norm_name(f.name), f)
    except OSError:
        return None
    for base in bases:
        for ext in (".nfo", ".json"):
            hit = existing.get(f"{str(base).strip()}{ext}".lower())
            if hit:
                return hit
    return None


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def rating_to_10(value: Any) -> float:
    """把片匣的 5 星评分换算为 Kodi/Emby 的 0-10 分制。

    片匣的 ``movies.rating`` 是用户自己的 5 星打分（见 store 里的评分分档），
    直接写进 NFO 会被媒体服务器按 10 分制显示，等于把评分砍半。
    """
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if v <= 0:
        return 0.0
    return round(min(10.0, v * 2), 1)


def _valid_date(value: Any) -> str:
    """只输出 YYYY-MM-DD，其余（空 / 只有年份 / 非法串）一律不写。

    媒体服务器对 ``<premiered>`` 格式很敏感，写错会导致条目识别失败。
    """
    s = str(value or "").strip()
    if _DATE_RE.match(s):
        return s
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", s)
    if m:
        y, mo, d = m.groups()
        return "%s-%02d-%02d" % (y, int(mo), int(d))
    return ""


def _year_of(movie: Dict[str, Any]) -> str:
    year = str(movie.get("year") or "").strip()
    if re.fullmatch(r"\d{4}", year):
        return year
    return _valid_date(movie.get("release_date"))[:4]


def build_nfo_root(movie: Dict[str, Any], poster: str = "", fanart: str = "",
                   rating_field: str = "userrating") -> ET.Element:
    """把影片记录构造成 Kodi/Emby 风格 ``<movie>`` 元素。

    poster / fanart 传**相对文件名**（如 ``ABC-123-poster.jpg``），不要传绝对路径
    或远程 URL：NFO 会跟视频一起被搬来搬去，绝对路径换机器即失效。

    rating_field 决定片匣的 5 星评分写到哪个标签：
      - ``userrating``（默认）：片匣评分本质是用户个人评分，语义正确；
      - ``rating``：写进站点评分位，各端都显示，但会与"官方评分"语义混淆；
      - ``both``：两处都写。
    """
    root = ET.Element("movie")

    def add(tag: str, value: Any) -> None:
        if value in (None, "", 0, 0.0):
            return
        ET.SubElement(root, tag).text = str(value)

    add("title", movie.get("title") or movie.get("code"))
    add("originaltitle", movie.get("original_title"))
    # num：AV 刮削圈的番号约定，Kodi/Emby 忽略但无害。
    # 注意绝不把番号写进 <id> —— 那是 Kodi 的库内主键，写错会造成条目错乱。
    add("num", movie.get("code"))
    add("plot", movie.get("plot"))
    add("premiered", _valid_date(movie.get("release_date")))
    add("year", _year_of(movie))
    add("runtime", movie.get("runtime"))
    add("studio", movie.get("studio"))
    add("maker", movie.get("studio"))
    add("label", movie.get("publisher"))
    add("director", movie.get("director"))

    score = rating_to_10(movie.get("rating"))
    if score:
        field = (rating_field or "userrating").lower()
        if field in ("rating", "both"):
            add("rating", score)
        if field in ("userrating", "both"):
            add("userrating", score)

    code = str(movie.get("code") or "").strip()
    if code:
        uid = ET.SubElement(root, "uniqueid")
        uid.set("type", "avm")
        uid.text = code

    if movie.get("series"):
        st = ET.SubElement(root, "set")
        ET.SubElement(st, "name").text = str(movie["series"])

    for g in movie.get("genres") or []:
        ET.SubElement(root, "genre").text = str(g)
    for t in movie.get("tags") or []:
        ET.SubElement(root, "tag").text = str(t)
    for a in movie.get("actresses") or []:
        actor = ET.SubElement(root, "actor")
        ET.SubElement(actor, "name").text = str(a)
        ET.SubElement(actor, "type").text = "Actor"

    if poster or fanart:
        art = ET.SubElement(root, "art")
        if poster:
            ET.SubElement(art, "poster").text = str(poster)
        if fanart:
            ET.SubElement(art, "fanart").text = str(fanart)

    return root


def pretty(root: ET.Element) -> str:
    """格式化输出 XML 字符串（UTF-8 声明 + 两空格缩进）。"""
    xml = ET.tostring(root, encoding="unicode")
    return minidom.parseString(xml).toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")


def build_nfo(movie: Dict[str, Any], poster: str = "", fanart: str = "",
              rating_field: str = "userrating") -> str:
    """生成 NFO 文本（兼容旧调用：不传图片时与历史行为一致）。"""
    return pretty(build_nfo_root(movie, poster=poster, fanart=fanart,
                                 rating_field=rating_field))


def read_nfo_root(path: str | Path) -> Optional[ET.Element]:
    """读取已有 NFO 的根元素；解析失败返回 None（调用方自行降级）。"""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    if not raw.strip():
        return None
    raw = raw[raw.find("<"):] if "<" in raw else raw
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        return None


def _child_key(node: ET.Element) -> tuple:
    """同一父节点下判断「是不是同一个子节点」的键。

    actor 按名字、genre/tag 按文本、uniqueid 按 type 合并，其余按标签名。
    这样重复刮削时不会把同一个女优/类型写成两条。
    """
    tag = node.tag
    if tag == "uniqueid":
        return (tag, node.get("type") or "")
    if tag == "actor":
        return (tag, (_text(node.find("name")) or "").lower())
    if tag in ("genre", "tag"):
        return (tag, (node.text or "").strip().lower())
    return (tag,)


def merge_into(existing: ET.Element, fresh: ET.Element) -> ET.Element:
    """以已有 NFO 为骨架，只用新数据**补空 + 补缺节点**，保留未知节点。

    这是"通用"的关键：Emby/Kodi 会在 NFO 里写 ``<streamdetails>``、``<fileinfo>``、
    ``<banner>`` 等本程序不产出的节点，纯重写会把这些技术信息全部抹掉。
    本函数只做叶子级合并，不动已有值（覆盖是另一个策略，由 writer 负责）。
    """
    index = {}
    for child in list(existing):
        index.setdefault(_child_key(child), child)

    for new_child in list(fresh):
        key = _child_key(new_child)
        old = index.get(key)
        if old is None:
            existing.append(new_child)
            index[key] = new_child
            continue
        # art 是容器节点：逐个图片槽位合并
        if new_child.tag == "art":
            have = {c.tag for c in list(old)}
            for slot in list(new_child):
                if slot.tag not in have and (slot.text or "").strip():
                    old.append(slot)
            continue
        # 已有值为空才补，非空一律不动（尊重用户的既有整理）
        if not (old.text or "").strip() and (new_child.text or "").strip():
            old.text = new_child.text
        # 子节点递归补齐（如 <set><name>）
        if len(new_child):
            for sub in list(new_child):
                if not len(old.findall(sub.tag)):
                    old.append(sub)
    return existing
