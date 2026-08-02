# -*- coding: utf-8 -*-
"""封面管理：本地嗅探、URL 下载、占位图生成。"""
from __future__ import annotations

import hashlib
import html
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from .config import COVER_DIR, avatar_dir, fanart_dir, ensure_dirs

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")

# 同目录下按优先级嗅探的候选文件名（{stem} 为视频文件主名）
_SIBLING_PATTERNS = [
    "{stem}-poster", "{stem}-cover", "{stem}-thumb", "{stem}-fanart", "{stem}",
    "poster", "cover", "folder", "movie", "default", "fanart", "thumb", "landscape",
]

_MAGIC = [
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF8", ".gif"),
    (b"BM", ".bmp"),
]


def safe_key(key: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(key))[:80]
    digest = hashlib.sha1(str(key).encode("utf-8", "ignore")).hexdigest()[:8]
    return f"{cleaned}_{digest}"


def cover_path(filename: str) -> Optional[Path]:
    if not filename:
        return None
    p = COVER_DIR / filename
    return p if p.exists() else None


def _guess_ext(data: bytes, url: str = "") -> str:
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    m = re.search(r"\.(jpe?g|png|webp|gif|bmp)(?:\?|$)", url, re.I)
    return f".{m.group(1).lower()}" if m else ".jpg"


# 已知占位图 / 无效封面黑名单：DMM 无封面占位图 jppl.jpg 等。
# 这些图文件头合法（能过魔数校验），但内容固定、不表示真实封面，必须拦截。
_PLACEHOLDER_HASHES = {
    # DMM 无封面占位图 jppl.jpg（约 19KB，纯色+logo，文件头合法但无真实内容）
    "8c6455760bf9c0c487142280fcef1877",
}
_PLACEHOLDER_URL_HINTS = ("jppl.jpg", "noimage", "no_image", "placeholder", "blank", "dummy", "default_cover")


def _is_placeholder(data: bytes, url: str = "") -> bool:
    """识别固定占位图（DMM jppl.jpg 等），避免把无封面占位图当真实封面落盘。"""
    if not data:
        return True
    u = (url or "").lower()
    if any(h in u for h in _PLACEHOLDER_URL_HINTS):
        return True
    # 过大/过小都可能是占位；用内容哈希精确命中已知占位图
    h = hashlib.md5(data).hexdigest()
    return h in _PLACEHOLDER_HASHES


def save_bytes(key: str, data: bytes, url: str = "") -> Optional[str]:
    if not data or len(data) < 512:
        return None
    if _is_placeholder(data, url):
        return None
    ensure_dirs()
    ext = _guess_ext(data, url)
    name = f"{safe_key(key)}{ext}"
    (COVER_DIR / name).write_bytes(data)
    _drop_other_exts(key, keep=name)
    return name


def save_local_file(key: str, src: Path) -> Optional[str]:
    try:
        if not src.exists() or src.stat().st_size < 512:
            return None
        ensure_dirs()
        name = f"{safe_key(key)}{src.suffix.lower()}"
        shutil.copyfile(src, COVER_DIR / name)
        _drop_other_exts(key, keep=name)
        return name
    except Exception:
        return None


def _drop_other_exts(key: str, keep: str) -> None:
    prefix = safe_key(key)
    for f in COVER_DIR.glob(f"{prefix}.*"):
        if f.name != keep:
            try:
                f.unlink()
            except Exception:
                pass


def find_local_cover(video_path: str | Path, code: str = "") -> Optional[Path]:
    """在视频同目录中查找可用作封面的图片。"""
    try:
        p = Path(video_path)
        folder = p.parent
        if not folder.is_dir():
            return None
        stem = p.stem
        candidates: list[str] = []
        for pat in _SIBLING_PATTERNS:
            candidates.append(pat.format(stem=stem))
        if code:
            candidates.extend([code, f"{code}-poster", f"{code}pl", f"{code}ps"])

        existing = {f.name.lower(): f for f in folder.iterdir() if f.is_file()}
        for base in candidates:
            for ext in IMAGE_EXTS:
                hit = existing.get(f"{base}{ext}".lower())
                if hit and hit.stat().st_size > 512:
                    return hit
        return None
    except Exception:
        return None


def download(url: str, cfg: Dict[str, Any]) -> Optional[bytes]:
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    try:
        import requests
    except ImportError:
        return None
    scraper = cfg.get("scraper", {})
    proxy = scraper.get("proxy") or ""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = requests.get(
            url,
            timeout=cfg.get("cover", {}).get("timeout", 20),
            headers={"User-Agent": scraper.get("user_agent", "Mozilla/5.0"), "Referer": url},
            proxies=proxies,
        )
        resp.raise_for_status()
        data = resp.content
        if not _looks_like_image(data):
            # 下载到的不是真实图片（可能是错误页 / 占位 GIF / 挑战页），丢弃避免错封面
            return None
        return data
    except Exception:
        return None


def _looks_like_image(data: bytes) -> bool:
    """用文件头魔数校验是否为真实图片，过滤掉 HTML 错误页 / 1x1 占位图等。"""
    if not data or len(data) < 512:
        return False
    head = data[:12]
    if head[:3] == b"\xff\xd8\xff":  # JPEG
        return True
    if head[:8] == b"\x89PNG\r\n\x1a\n":  # PNG
        return True
    if head[:6] in (b"GIF87a", b"GIF89a"):  # GIF
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":  # WEBP
        return True
    return False


_PALETTE = [
    ("#1f2a44", "#4c6ef5"), ("#2b1f44", "#9775fa"), ("#44201f", "#ff6b6b"),
    ("#1f4436", "#20c997"), ("#443b1f", "#fcc419"), ("#1f3944", "#22b8cf"),
    ("#3d1f44", "#e64980"), ("#2f2f2f", "#adb5bd"),
]


def placeholder_svg(code: str, title: str = "") -> str:
    """无封面时生成一张带番号的纯色占位图（SVG 文本）。"""
    label = (code or title or "NO IMAGE").strip()
    idx = int(hashlib.md5(label.encode("utf-8", "ignore")).hexdigest(), 16) % len(_PALETTE)
    bg, accent = _PALETTE[idx]
    main = html.escape(label[:16])
    sub = html.escape((title or "")[:22])
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="380" height="540" viewBox="0 0 380 540">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{bg}"/>
      <stop offset="100%" stop-color="#11131a"/>
    </linearGradient>
  </defs>
  <rect width="380" height="540" fill="url(#g)"/>
  <rect x="18" y="18" width="344" height="504" fill="none" stroke="{accent}" stroke-opacity="0.16" stroke-width="1.5" rx="10"/>
  <circle cx="190" cy="214" r="58" fill="none" stroke="{accent}" stroke-opacity="0.18" stroke-width="2.5"/>
  <path d="M172 192 L214 214 L172 236 Z" fill="{accent}" fill-opacity="0.35"/>
  <text x="190" y="330" font-family="Segoe UI, Microsoft YaHei, sans-serif" font-size="28"
        font-weight="600" fill="#f1f3f5" text-anchor="middle" fill-opacity="0.92">{main}</text>
  <text x="190" y="364" font-family="Segoe UI, Microsoft YaHei, sans-serif" font-size="14"
        fill="#adb5bd" text-anchor="middle" fill-opacity="0.7">{sub}</text>
  <text x="190" y="492" font-family="Segoe UI, Microsoft YaHei, sans-serif" font-size="11"
        fill="{accent}" fill-opacity="0.45" text-anchor="middle" letter-spacing="4">NO COVER</text>
</svg>"""


# ------------------------------------------------------------------ 女优头像 / 背景大图（fanart）落盘
#
# 这类资源默认只存远程 URL（节省空间、规避墙内代理下载不便），可在设置中开启
# "下载落盘"。落盘目录支持自定义（config.media.avatar_dir / fanart_dir），可为
# 绝对路径，便于把媒体资源放到独立磁盘或整体迁移。db 中仅存相对文件名，路径变更
# 不影响数据，只需改配置即可重新指向。


def is_remote(value: str) -> bool:
    """判断头像/背景字段是远程 URL 还是本地相对文件名。"""
    return bool(value) and str(value).lower().startswith(("http://", "https://"))


def _resolve_location(location: str, cfg: Dict[str, Any]) -> Optional[bytes]:
    """location 可以是远程 URL（需下载）或本地图片路径（直接读）。返回图片字节或 None。"""
    if not location:
        return None
    if is_remote(location):
        return download(location, cfg)
    p = Path(location)
    if p.exists() and p.is_file() and p.stat().st_size > 512:
        try:
            return p.read_bytes()
        except Exception:
            return None
    return None


def save_avatar(name: str, location: str, cfg: Dict[str, Any]) -> Optional[str]:
    """保存女优头像。name 通常为女优名（作为稳定 key）。返回相对文件名。"""
    data = _resolve_location(location, cfg)
    if not data:
        return None
    d = avatar_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    ext = _guess_ext(data, location if is_remote(location) else "")
    fname = f"{safe_key(name)}{ext}"
    (d / fname).write_bytes(data)
    return fname


def save_fanart(key: str, location: str, cfg: Dict[str, Any]) -> Optional[str]:
    """保存影片背景大图（fanart）。key 通常为番号。返回相对文件名。"""
    data = _resolve_location(location, cfg)
    if not data:
        return None
    d = fanart_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    ext = _guess_ext(data, location if is_remote(location) else "")
    fname = f"{safe_key(key)}{ext}"
    (d / fname).write_bytes(data)
    return fname


def avatar_path(filename: str) -> Optional[Path]:
    if not filename or is_remote(filename):
        return None
    p = avatar_dir() / filename
    return p if p.exists() else None


def fanart_path(filename: str) -> Optional[Path]:
    if not filename or is_remote(filename):
        return None
    p = fanart_dir() / filename
    return p if p.exists() else None

