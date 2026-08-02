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
        existing = {f.name.lower(): f for f in folder.iterdir() if f.is_file()}
    except OSError:
        return None
    for base in bases:
        for ext in (".nfo", ".json"):
            hit = existing.get(f"{base}{ext}".lower())
            if hit:
                return hit
    return None


def build_nfo(movie: Dict[str, Any]) -> str:
    root = ET.Element("movie")

    def add(tag: str, value: Any) -> None:
        if value in (None, "", 0):
            return
        ET.SubElement(root, tag).text = str(value)

    add("title", movie.get("title") or movie.get("code"))
    add("originaltitle", movie.get("original_title"))
    add("num", movie.get("code"))
    add("plot", movie.get("plot"))
    add("premiered", movie.get("release_date"))
    add("year", movie.get("year"))
    add("runtime", movie.get("runtime"))
    add("studio", movie.get("studio"))
    add("label", movie.get("publisher"))
    add("director", movie.get("director"))
    add("rating", movie.get("rating"))

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

    art = ET.SubElement(root, "art")
    if movie.get("cover_url"):
        ET.SubElement(art, "poster").text = movie["cover_url"]
    if movie.get("fanart_url"):
        ET.SubElement(art, "fanart").text = movie["fanart_url"]

    xml = ET.tostring(root, encoding="unicode")
    return minidom.parseString(xml).toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")
