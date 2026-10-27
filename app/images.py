# -*- coding: utf-8 -*-
"""封面管理：本地嗅探、URL 下载、占位图生成。"""
from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .config import COVER_DIR, avatar_dir, fanart_dir, ensure_dirs, outbound_ssl_context

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


# ---------------------------------------------------------------- 竖版海报（poster）
#
# 三级来源，质量从高到低：
#   1. **DMM 官方竖版原图** `{番号}ps.jpg`（1000–1540px 宽，真·海报）——最清晰；
#   2. 横版封面按策略裁出 2:3（默认取右侧）——离线可用，兜底；
#   3. 原生竖版封面（FC2 等）直接归一化到 2:3。
# 原图（横版完整封面）始终不动：详情页 / fanart 导出用完整图。
# 文件名由 cover 推导 + 同目录 `.poster.json` 记策略来源，策略变更自动失效重生成。

POSTER_SUFFIX = "_poster.jpg"
POSTER_META_SUFFIX = "_poster.json"
# right = 取右侧（默认，人物主视觉常见位置）；center = 居中；fit = 完整缩放+模糊背景补边
POSTER_STRATEGIES = ("right", "center", "fit")
_POSTER_MAX_H = 1400          # 上限：DMM 官方竖版通常 1032–1534 宽，控制在一倍屏够用且体积可控


def poster_name(cover_file: str) -> str:
    """由封面文件名推导竖版海报文件名。"""
    return Path(str(cover_file)).stem + POSTER_SUFFIX


def poster_path(cover_file: str) -> Optional[Path]:
    """返回已生成的竖版海报路径（不存在返回 None）。"""
    if not cover_file:
        return None
    p = COVER_DIR / poster_name(cover_file)
    return p if p.exists() else None


def _poster_meta_path(cover_file: str) -> Path:
    return COVER_DIR / (Path(str(cover_file)).stem + POSTER_META_SUFFIX)


def _read_meta(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# ---- DMM 官图：番号 -> awsimgsrc 路径（ps = 竖版海报，pl = 横版封面） ----

def _dmm_code(code: str) -> str:
    """番号归一化成 DMM 路径名：JUL-185 -> jul00185（数字补足 5 位）。

    FC2 / 素人 / 无码等库里没有统一 awsimgsrc 编号的，直接放弃（返回空）。
    """
    s = str(code or "").strip().lower()
    if not s or s.startswith("fc2"):
        return ""
    m = re.match(r"^((?:\d+)?[a-z]+)[-_ ]?0*(\d{1,6})(?:[-_ ]?\d+)?$", s)
    if not m:
        return ""
    return "%s%05d" % (m.group(1), int(m.group(2)))


def dmm_urls(code: str) -> Optional[Dict[str, str]]:
    c = _dmm_code(code)
    if not c:
        return None
    base = "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/%s/%s" % (c, c)
    return {"poster": base + "ps.jpg", "cover": base + "pl.jpg"}


def _open_image(src: Path) -> Optional[Any]:
    try:
        from PIL import Image, ImageOps
        im = Image.open(src)
        return ImageOps.exif_transpose(im).convert("RGB")
    except Exception:
        return None


def _fit_canvas(im: Any) -> Any:
    """整图缩进 2:3 画布居中，空白用模糊放大的原图填充（不丢信息）。"""
    from PIL import Image, ImageFilter
    w, h = im.size
    canvas_h = min(_POSTER_MAX_H * 2, max(h, 1400))
    canvas_w = round(canvas_h * 2 / 3)
    bg = im.resize((canvas_w, canvas_h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(24))
    bg = Image.eval(bg, lambda v: int(v * 0.68))
    scale = min(canvas_w / w, canvas_h / h)
    fg = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    bg.paste(fg, ((canvas_w - fg.width) // 2, (canvas_h - fg.height) // 2))
    return bg


# 裁切后窄于这个宽度就认为「裁出来会糊」：800×540 的横版裁 2:3 只有 360 宽，
# 放到卡片上等于把 360px 放大到 2x 屏 —— 不如保留 800 宽、两侧模糊补边
POSTER_CROP_MIN_W = 520


def _render_poster(im: Any, strategy: str) -> Any:
    """把任意比例的图渲染成精确 2:3 竖版（不裁主体、不放大）。"""
    w, h = im.size
    if h >= w * 1.15:
        # 原生竖版：只裁掉两侧多余的 1–2 像素级边，绝不裁内容区
        tw = min(w, round(h * 2 / 3))
        x0 = (w - tw) // 2
        return im.crop((x0, 0, x0 + tw, h))
    if strategy == "fit" or (0.85 <= h / w < 1.15):
        # 方形 / 接近方形的封面裁 2:3 会切掉 1/3 宽，主体必断——直接补边
        return _fit_canvas(im)
    crop_w = min(w, max(1, round(h * 2 / 3)))
    x0 = (w - crop_w) // 2 if strategy == "center" else (w - crop_w)
    return im.crop((x0, 0, x0 + crop_w, h))


def _save_poster(im: Any, dst: Path) -> bool:
    """保存为 2:3 JPEG；只缩小不放大（源小就保持原尺寸，糊图放大只会更糊）。"""
    try:
        from PIL import Image
        out = im
        if out.height > _POSTER_MAX_H:
            target_w = max(1, round(out.width * _POSTER_MAX_H / out.height))
            out = out.resize((target_w, _POSTER_MAX_H), Image.LANCZOS)
        elif out.width < POSTER_CROP_MIN_W:
            # 低清裁切偏窄（800×540 裁出 360 宽）：温和放大填满卡片，
            # 上限 1.45 倍——再多只是把糊图摊大
            scale = min(1.45, POSTER_CROP_MIN_W / max(1, out.width))
            if scale > 1.05:
                out = out.resize((round(out.width * scale), round(out.height * scale)), Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        out.save(dst, "JPEG", quality=88, optimize=True, progressive=True)
        return True
    except Exception:
        return False


def _effective_strategy(im: Any, strategy: str) -> str:
    """实际会采用的策略（低清横版自动 fit、方形自动 fit）。用于写元信息。"""
    w, h = im.size
    if h >= w * 1.15:
        return "portrait"
    if strategy == "fit" or (0.85 <= h / w < 1.15):
        return "fit"
    return strategy


def make_poster(src: Path, dst: Path, strategy: str = "right",
                meta: Dict[str, Any] | None = None) -> bool:
    """从图片文件生成 2:3 竖版海报（离线兜底路径）。"""
    im = _open_image(src)
    if im is None or im.width < 40 or im.height < 40:
        return False
    effective = _effective_strategy(im, strategy)
    out = _render_poster(im, strategy)
    if not _save_poster(out, dst):
        return False
    info = {"from": "cover", "strategy": effective,
            "src_w": im.width, "src_h": im.height,
            "src_mtime": src.stat().st_mtime}
    if meta:
        info.update(meta)
    _write_meta(COVER_DIR / (Path(str(src.name)).stem + POSTER_META_SUFFIX), info)
    return True


def save_poster_from_bytes(data: bytes, cover_file: str,
                           cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """DMM 官方竖版图落盘为海报（归一化到 2:3，不放大）。"""
    try:
        from io import BytesIO
        from PIL import Image, ImageOps
        im = Image.open(BytesIO(data))
        im = ImageOps.exif_transpose(im).convert("RGB")
    except Exception:
        return None
    dst = COVER_DIR / poster_name(cover_file)
    out = _render_poster(im, "right")
    if not _save_poster(out, dst):
        return None
    src = cover_path(cover_file)
    _write_meta(COVER_DIR / (Path(str(cover_file)).stem + POSTER_META_SUFFIX),
                {"from": "dmm", "src_w": im.width, "src_h": im.height,
                 "src_mtime": src.stat().st_mtime if src else 0})
    return dst.name



# ---- DMM 无图缓存：素人/无码番号在 DMM 没有条目，反复探测只会白等超时 ----
_DMM_MISS_FILE = ".dmm_missing.json"
_DMM_MISS_TTL = 7 * 24 * 3600


def _dmm_miss_load() -> Dict[str, float]:
    try:
        data = json.loads((COVER_DIR / _DMM_MISS_FILE).read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in (data or {}).items() if isinstance(v, (int, float))}
    except Exception:
        return {}


def _dmm_miss_get(code: str) -> bool:
    cid = _dmm_code(code)
    if not cid:
        return True
    ts = _dmm_miss_load().get(cid) or 0
    return (time.time() - ts) < _DMM_MISS_TTL


def _dmm_miss_add(code: str) -> None:
    cid = _dmm_code(code)
    if not cid:
        return
    data = _dmm_miss_load()
    data[cid] = time.time()
    try:
        (COVER_DIR / _DMM_MISS_FILE).write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def dmm_available(code: str) -> bool:
    """该番号近期是否值得再向 DMM 探测（避免反复超时）。"""
    return bool(_dmm_code(code)) and not _dmm_miss_get(code)



# 低于这个宽度视为「低清封面」：允许被明显更好的图替换（重刮时不被历史低清图锁死）
COVER_CLEAR_MIN_W = 600
# 新图面积要达到旧图的这个倍数才算「更好的封面」
COVER_UPGRADE_RATIO = 1.6


def bytes_size(data: bytes) -> Optional[Tuple[int, int]]:
    """从字节流读图片尺寸（解不出返回 None）。"""
    try:
        from io import BytesIO

        from PIL import Image
        with Image.open(BytesIO(data)) as im:
            return (im.width, im.height)
    except Exception:
        return None


def file_size(path: Optional[Path]) -> Optional[Tuple[int, int]]:
    if path is None or not Path(path).exists():
        return None
    try:
        from PIL import Image
        with Image.open(path) as im:
            return (im.width, im.height)
    except Exception:
        return None


def cover_size(cover_file: str) -> Optional[Tuple[int, int]]:
    """当前封面（data/covers 下）的像素尺寸。"""
    return file_size(cover_path(cover_file))


def better_cover(new: Optional[Tuple[int, int]], old: Optional[Tuple[int, int]]) -> bool:
    """新封面是否值得替换旧封面：按像素面积判断，低清老图优先让位。

    * 没有旧图 → 用新的；
    * 新图面积 ≥ 旧图 1.6 倍 → 升级；
    * 旧图宽度 < COVER_CLEAR_MIN_W（低清）且新图更宽 → 升级。
    这样既不会被 360×540 的本地图锁死，也不会把 2184×1468 的官方大图换成站点小图。
    """
    if new is None:
        return False
    if old is None:
        return True
    nw, nh = new
    ow, oh = old
    if nw * nh >= ow * oh * COVER_UPGRADE_RATIO:
        return True
    if ow < COVER_CLEAR_MIN_W and nw > ow:
        return True
    return False


def poster_dmm_enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """是否允许向 DMM 抓官方竖版（设置 → 媒体库 → 竖版海报）。"""
    try:
        from .config import load_config
        media = ((cfg or load_config()).get("media") or {})
        return bool(media.get("poster_dmm", True))
    except Exception:
        return True


def _poster_strategy(cfg: Optional[Dict[str, Any]]) -> str:
    try:
        from .config import load_config
        val = str(((cfg or load_config()).get("media") or {}).get("poster_strategy") or "right")
    except Exception:
        val = "right"
    return val if val in POSTER_STRATEGIES else "right"


def ensure_poster(cover_file: str, cfg: Optional[Dict[str, Any]] = None,
                  code: str = "", allow_network: bool = False) -> Optional[str]:
    """确保竖版海报存在（必要时重生成）。返回海报文件名，失败返回 None。

    失效条件：海报缺失 / 源图更新 / 策略变更（官方竖版不受策略影响）/
    允许联网但当前还不是官方竖版。
    """
    if not cover_file:
        return None
    src = cover_path(cover_file)
    if src is None:
        return None
    dst = COVER_DIR / poster_name(cover_file)
    meta = _read_meta(COVER_DIR / (Path(str(cover_file)).stem + POSTER_META_SUFFIX))
    strategy = _poster_strategy(cfg)
    from_dmm = meta.get("from") == "dmm"
    stale = (
        not dst.exists()
        or float(meta.get("src_mtime") or 0) < src.stat().st_mtime
        or (not from_dmm and meta.get("strategy") != strategy)
        or (allow_network and bool(code) and not from_dmm)
    )
    if not stale:
        return dst.name

    # 1) 优先 DMM 官方竖版（真海报，比裁横图清晰一个档次）
    if allow_network and code and dmm_available(code):
        urls = dmm_urls(code)
        if urls:
            data = download(urls["poster"], cfg or {}, referer="https://www.dmm.co.jp/")
            if data:
                name = save_poster_from_bytes(data, cover_file)
                if name:
                    return name
            else:
                _dmm_miss_add(code)

    # 2) 兜底：从封面裁 / 归一化
    ok = make_poster(src, dst, strategy)
    return dst.name if ok else None


def upgrade_from_dmm(key: str, code: str, cfg: Dict[str, Any],
                     cover_file: str = "") -> Dict[str, str]:
    """用 DMM 高清横版（pl）替换当前封面，**仅当确实更清晰时**。

    竖版海报不在这里处理：``ensure_poster(allow_network=True)`` 会优先取官方 ps，
    没有官方竖版才从（此时已经升级过的）横版裁切——顺序上必须先升级封面再生成海报。
    只在刮削路径调用，手工上传 / 抽帧不覆盖用户选择。
    """
    urls = dmm_urls(code)
    if not urls or not dmm_available(code):
        return {}
    out: Dict[str, str] = {}
    data = download(urls["cover"], cfg, referer="https://www.dmm.co.jp/")
    if data:
        new_size = bytes_size(data)
        cur_size = cover_size(cover_file) if cover_file else None
        if better_cover(new_size, cur_size):
            name = save_bytes(key, data, urls["cover"])
            if name:
                out["cover"] = name
        else:
            # 已有更大的图：记住「DMM 查过」避免每次重复请求
            pass
    if not out.get("cover"):
        _dmm_miss_add(code)
    return out


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


def download(url: str, cfg: Dict[str, Any], referer: str = "") -> Optional[bytes]:
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    scraper = cfg.get("scraper", {})
    proxy = scraper.get("proxy") or ""
    timeout = cfg.get("cover", {}).get("timeout", 20)
    ref = referer or url
    low_url = url.lower()
    # javbus 搜索结果图床（cloudfront）需带 javbus referer 防盗链
    if "cloudfront.net" in low_url and not referer:
        ref = "https://www.javbus.com/"
    headers = {
        "User-Agent": scraper.get("user_agent", "Mozilla/5.0"),
        "Referer": ref,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }

    data = _download_urllib(url, headers, proxy, timeout)
    if data is None:
        data = _download_requests(url, headers, proxy, timeout)
    if data is None:
        return None
    if not _looks_like_image(data):
        # 下载到的不是真实图片（可能是错误页 / 占位 GIF / 挑战页），丢弃避免错封面
        return None
    return data


def _download_requests(url: str, headers: Dict[str, str], proxy: str, timeout: int) -> Optional[bytes]:
    """requests 方式下载（回退用）。"""
    try:
        import requests
    except ImportError:
        return None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = requests.get(url, timeout=timeout, headers=headers, proxies=proxies)
        resp.raise_for_status()
        return resp.content
    except Exception:
        return None


def _download_urllib(url: str, headers: Dict[str, str], proxy: str, timeout: int) -> Optional[bytes]:
    """urllib（标准库）方式下载。

    优先使用 urllib 是因为部分本地代理会对 requests/urllib3 的 TLS 指纹返回 403，
    而 urllib 的标准 TLS 指纹可通过（与女优资料维基插件同理）。
    """
    import gzip
    import io
    import urllib.parse
    import urllib.request
    try:
        # 中文等非 ASCII 路径需百分号编码，否则 urllib 构造请求时报 UnicodeEncodeError。
        # header（如 Referer）里的 URL 也须同步编码，否则 urllib 用 latin-1 编码 header 会崩。
        if any(ord(c) > 127 for c in url):
            enc_url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%")
            for k, v in list(headers.items()):
                if v == url:
                    headers[k] = enc_url
            url = enc_url
        proxy_handler = urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy} if proxy else {})
        # 证书校验默认开启（出站中间人防护）；只有用户显式设置
        # server.insecure_skip_verify = true 时才降级为不校验。
        ctx = outbound_ssl_context()
        opener = urllib.request.build_opener(
            proxy_handler, urllib.request.HTTPSHandler(context=ctx))
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        return raw
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

