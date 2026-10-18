# -*- coding: utf-8 -*-
"""字幕匹配与对齐。

解决一个真实痛点：用户下载的字幕包里，字幕文件名往往和视频文件名不一致
（如只有中文片名、或带字幕组前缀），导致 PotPlayer/mpv 等系统播放器无法自动
加载。本模块把字幕按"番号"模糊匹配到库内影片，并自动重命名对齐到视频同目录，
让播放器打开即加载——无需在浏览器内渲染字幕（原生 <video> 对中字支持极差）。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import parser, store
from .db import query_all, query_one

# 常见字幕扩展名
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".smi", ".txt"}

# 语言猜测（基于文件名常见标记）
_LANG_HINTS = [
    ("chs", "简体中文"), ("gb", "简体中文"), ("sc", "简体中文"),
    ("cht", "繁体中文"), ("big5", "繁体中文"), ("tc", "繁体中文"),
    ("zh", "中文"), ("cn", "中文"), ("chinese", "中文"),
    ("ja", "日文"), ("jp", "日文"), ("jpn", "日文"),
    ("en", "英文"), ("eng", "英文"), ("english", "英文"),
]


def _lang_of(name: str) -> str:
    low = name.lower()
    for token, label in _LANG_HINTS:
        if token in low:
            return label
    return "未知"


def discover_subtitles(root: str) -> List[Dict[str, str]]:
    """递归收集 root 目录下所有字幕文件（路径 + 文件名）。"""
    files: List[Dict[str, str]] = []
    root_path = Path(root)
    if not root_path.exists():
        return files
    for p in root_path.rglob("*"):
        if p.is_file() and p.suffix.lower() in SUBTITLE_EXTS:
            files.append({"path": str(p), "filename": p.name})
    return files


def _match_code(p: Path) -> Optional[str]:
    """从字幕文件名/所在目录名抽取番号（复用视频文件的解析逻辑）。"""
    parsed = parser.parse_file(str(p))
    return parsed.get("code") or ""


def _movie_index(conn) -> Dict[str, List[dict]]:
    """建立 番号(大写) -> movie 列表 的索引。"""
    movies = query_all(
        conn,
        "SELECT id, code, key, title FROM movies WHERE has_code = 1 AND code <> ''",
    )
    code_map: Dict[str, List[dict]] = {}
    for m in movies:
        code_map.setdefault(m["code"].upper(), []).append(m)
    return code_map


def _classify_one(conn, code_map: Dict[str, List[dict]], path: str):
    """匹配单个字幕文件，返回 (matched_dict|None, unmatched_dict|None)。"""
    sp = Path(path)
    code = _match_code(sp)
    if code:
        cands = code_map.get(code.upper())
        if cands:
            vid = store.movie_primary_file(conn, cands[0]["id"])
            return {
                "subtitle_path": path,
                "subtitle_name": sp.name,
                "lang": _lang_of(sp.name),
                "movie_id": cands[0]["id"],
                "code": code,
                "title": cands[0]["title"],
                "video_path": vid,
            }, None
    return None, {
        "subtitle_path": path,
        "subtitle_name": sp.name,
        "lang": _lang_of(sp.name),
        "code": code,
    }


def match_subtitles_to_movies(conn, subtitle_dir: str) -> Dict[str, Any]:
    """扫描字幕目录，按番号匹配库内影片。

    返回 matched（可直接对齐）/ unmatched（抽取不到番号，需人工确认）。
    不改动磁盘与数据库，仅做探测，供前端展示确认。
    """
    subs = discover_subtitles(subtitle_dir)
    code_map = _movie_index(conn)
    matched: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    for s in subs:
        m, u = _classify_one(conn, code_map, s["path"])
        if m:
            matched.append(m)
        else:
            unmatched.append(u)
    return {"matched": matched, "unmatched": unmatched, "total": len(subs)}


def match_subtitle_files(conn, paths: List[str]) -> Dict[str, Any]:
    """针对用户手工选定的若干字幕文件做匹配（不扫描整目录）。"""
    code_map = _movie_index(conn)
    matched: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    for p in paths:
        p = str(p).strip()
        if not p or not os.path.isfile(p):
            unmatched.append({"subtitle_path": p, "subtitle_name": os.path.basename(p),
                              "lang": _lang_of(p), "code": ""})
            continue
        m, u = _classify_one(conn, code_map, p)
        if m:
            matched.append(m)
        else:
            unmatched.append(u)
    return {"matched": matched, "unmatched": unmatched, "total": len(paths)}


def _register_subtitle(conn, movie_id: int, path: str, source: str = "matched") -> bool:
    """登记一条字幕（幂等）。返回是否真的新增。"""
    existing = query_one(
        conn,
        "SELECT id FROM movie_subtitles WHERE movie_id=? AND file_path=?",
        (movie_id, path),
    )
    if existing is not None:
        return False
    conn.execute(
        "INSERT INTO movie_subtitles(movie_id, file_path, filename, lang, source) "
        "VALUES(?,?,?,?,?)",
        (movie_id, path, os.path.basename(path), _lang_of(os.path.basename(path)), source),
    )
    return True


# 自动登记外挂字幕时接受的扩展名。故意不含 .txt：同目录的说明文件太容易被误当字幕。
SIDECAR_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".smi"}


def _norm_key(name: str) -> str:
    """归一化文件名主干：小写并去掉分隔符，让 JUL185C.srt 匹配 jul-185-C.mp4。"""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _sidecar_files(folder: Path) -> List[Path]:
    try:
        return [p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in SIDECAR_EXTS]
    except OSError:
        return []


def _dir_video_count(folder: Path, exts: set) -> int:
    """目录里有多少个视频文件：决定能否用「单视频目录」兜底规则。"""
    try:
        return sum(1 for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in exts)
    except OSError:
        return 0


def register_sidecars(conn, movie_id: int, video_paths: List[str], code: str = "") -> int:
    """把与视频同目录、能确定归属的外挂字幕登记进库（幂等），返回新增条数。

    归属判定从强到弱：
    1. 字幕主干等于视频主干，或以视频主干开头（`video.chs.srt`、`video.zh-CN.srt`）；
    2. 字幕主干包含该片番号（`jul185.srt` → JUL-185）；
    3. 目录里只有一个视频文件时，目录内其余字幕都归给它（应付「字幕.srt」这种随意命名）。

    同目录有多部影片时不做第 3 条，避免把 A 片的字幕登记到 B 片身上。
    """
    from .config import load_config, video_exts

    vids = [Path(p) for p in (video_paths or []) if p]
    if not vids:
        return 0
    by_dir: Dict[Path, List[Path]] = {}
    for v in vids:
        by_dir.setdefault(v.parent, []).append(v)

    exts = video_exts(load_config())
    code_key = _norm_key(code)
    added = 0
    for folder, vlist in by_dir.items():
        files = _sidecar_files(folder)
        if not files:
            continue
        vkeys = [(v, _norm_key(v.stem)) for v in vlist]
        # 用磁盘上真实的视频数量判断，而不是调用方传进来的路径数量：
        # 扫描器按「影片」调用，一个合集目录里可能有别的影片。
        only_one = _dir_video_count(folder, exts) <= 1
        for sp in files:
            sk = _norm_key(sp.stem)
            if not sk:
                continue
            # 0 字节字幕（下载失败 / 占位文件）登记后只会得到一条空轨，直接跳过
            try:
                if sp.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            owner: Optional[Path] = None
            for v, vk in vkeys:
                if vk and (sk == vk or sk.startswith(vk)):
                    owner = v
                    break
            if owner is None and code_key and code_key in sk:
                owner = vlist[0]
            if owner is None and only_one:
                owner = vlist[0]
            if owner is None:
                continue
            if _register_subtitle(conn, movie_id, str(sp), "sidecar"):
                added += 1
    return added


def align_subtitle(conn, subtitle_path: str, movie_id: int,
                   video_path: Optional[str] = None, copy: bool = False) -> Dict[str, Any]:
    """把单个字幕对齐到影片：重命名为视频同名并放到视频同目录（或复制）。

    - copy=False（默认）：仅当字幕与视频同目录时按视频名重命名（原地对齐）。
      若字幕在别的目录，则复制到视频同目录（保持原文件不动）。
    - 返回对齐后的字幕路径，并登记到 movie_subtitles 表。
    """
    video = video_path or store.movie_primary_file(conn, movie_id)
    if not video or not os.path.exists(video):
        raise FileNotFoundError("影片主视频不存在，无法对齐字幕")
    vid_path = Path(video)
    sub_path = Path(subtitle_path)
    target_dir = vid_path.parent
    target_name = vid_path.stem + sub_path.suffix.lower()
    target_path = target_dir / target_name

    if sub_path.resolve() == target_path.resolve():
        # 已是同名同目录，直接登记
        _register_subtitle(conn, movie_id, str(sub_path), "matched")
        return {"ok": True, "path": str(sub_path), "already": True}

    if copy or sub_path.parent.resolve() != target_dir.resolve():
        # 复制到视频同目录并重命名（原字幕包不动）
        shutil.copy2(sub_path, target_path)
        _register_subtitle(conn, movie_id, str(target_path), "matched")
        return {"ok": True, "path": str(target_path), "copied": True}

    # 同目录但文件名不同：直接重命名
    os.rename(sub_path, target_path)
    _register_subtitle(conn, movie_id, str(target_path), "matched")
    return {"ok": True, "path": str(target_path), "renamed": True}


def list_subtitles(conn, movie_id: int) -> List[Dict[str, Any]]:
    rows = query_all(
        conn,
        "SELECT id, file_path, filename, lang, source, created_at "
        "FROM movie_subtitles WHERE movie_id=? ORDER BY id",
        (movie_id,),
    )
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "path": r["file_path"], "filename": r["filename"],
            "lang": r["lang"], "source": r["source"], "created_at": r["created_at"],
            "exists": os.path.exists(r["file_path"]),
        })
    return out
