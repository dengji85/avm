"""HLS 分片内存缓存 + 顺序预取：让「起播 / 回看 / 拖动 / 连续播放」少等几次回源。

为什么需要：在线来源的分片要从源站拉，单段 1–3 MB。原生 HLS / hls.js 是顺序取片的，
只要源站单段响应慢，画面就会周期性停顿；回看已经播过的片段又要重新下载一遍。
这里做三件事：

1. **内存缓存**：把播过的分片留在内存（LRU + TTL），再取同一段直接命中，不走网络；
2. **磁盘缓存**：内存里的那点只够「正在播的这一段前后」，退出播放器就没了。开着磁盘层
   就把分片顺手写到 ``data/stream_cache/``，下次再看同一部（甚至重启后）命中磁盘，
   几乎零等待；总量由 ``stream.disk_cache_mb`` 控制，超了按最久未访问淘汰；
3. **预取**：收到第 i 段请求时，按最近一次播放列表里的顺序，后台取 i+1、i+2，
   等播放器真的要到它们时，字节已经在缓存里。

明确不做（避免变成坑）：
* 不当转码器；缓存只存原样字节，读出来直接吐；
* 带 Range 的请求不缓存：那是外部播放器拖动定位，字节区间不连续；
* 非分片内容不缓存：播放列表、AES 密钥、直链 MP4 一律直连上游；
* 单段超过 8 MB 不缓存（真人 4K 源的分片可能这么大，缓存收益低、挤占内存）；
* 磁盘键只用 scheme + host + path（**丢掉查询串**）：签名地址每次播放都变，
  带着签名当键等于永远不命中；分片内容对同一条路径是不变的，这样回看才能命中；
* 预取失败（签名过期 / 403 / 断流）静默放弃，不影响这次播放，也不重试。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urldefrag, urljoin, urlsplit

# 单段上限：超过就直接放弃缓存（大分片收益低，还容易把内存挤爆）
MAX_ITEM = 8 * 1024 * 1024
# 播放列表顺序的保留时长：签名地址通常十几分钟就失效，留半小时足够定位相邻段
_ORDER_TTL = 30 * 60
# 视为「分片」的扩展名（伪装扩展名的分片靠首字节 0x47 兜底识别）
_SEG_EXTS = {".ts", ".m4s", ".aac", ".m4a", ".mp3", ".vtt"}
# 磁盘文件后缀 → 回吐时的 Content-Type（磁盘层没有别处记类型，靠后缀还原）
_MEDIA_BY_EXT = {".ts": "video/mp2t", ".m4s": "video/iso.segment",
                 ".aac": "audio/aac", ".m4a": "audio/mp4",
                 ".mp3": "audio/mpeg", ".vtt": "text/vtt"}

_LOCK = threading.Lock()
# url -> (bytes, media_type, expire_at)
_CACHE: "OrderedDict[str, Tuple[bytes, str, float]]" = OrderedDict()
_BYTES = 0
# movie_id -> (分片绝对地址列表, 记录时间)
_ORDER: Dict[int, Tuple[List[str], float]] = {}
# movie_id -> 每段时长（秒），与 _ORDER 的下标一一对应；用于把「已缓存第几段」换算成时间
_DUR: Dict[int, List[float]] = {}
# movie_id -> 最近一次真正被请求的分片下标（滚动预读的窗口基准）
_LAST_IDX: Dict[int, int] = {}
# 正在预取的分片，避免同一段被重复排队
_PENDING: set = set()
# 正在跑的预取任务数：并发上限（stream.prefetch_workers）由它守住
_ACTIVE = 0

# ---- 磁盘层（可选）----
_DISK_LOCK = threading.RLock()
# 已在磁盘上的文件名：进度条判断「这段在不在磁盘里」用它，免得每 2 秒全盘 stat
_DISK_NAMES: Set[str] = set()
# 目录总占用；None = 还没扫过（首次读写时算一次，之后按增量维护）
_DISK_BYTES: Optional[int] = None


def _stream_cfg() -> Dict[str, Any]:
    from .config import load_config
    return load_config().get("stream") or {}


def enabled() -> bool:
    return bool(_stream_cfg().get("cache_segments", True))


def _limit_bytes() -> int:
    try:
        mb = int(_stream_cfg().get("cache_mb", 64))
    except (TypeError, ValueError):
        mb = 64
    return max(0, mb) * 1024 * 1024


def cacheable(url: str, media: str = "") -> bool:
    """这条地址值不值得缓存：只看分片 / 外挂字幕，别把播放列表和 MP4 直链塞进来。"""
    ext = os.path.splitext(urlsplit(url).path)[1].lower()
    if ext in _SEG_EXTS:
        return True
    mt = (media or "").lower()
    return mt.startswith(("video/mp2t", "video/iso.segment"))


# ---------------- 磁盘层 ----------------

def disk_limit() -> int:
    """磁盘缓存上限（字节）；0 = 关闭。"""
    try:
        mb = int(_stream_cfg().get("disk_cache_mb", 512))
    except (TypeError, ValueError):
        mb = 512
    return max(0, mb) * 1024 * 1024


def disk_enabled() -> bool:
    return bool(_stream_cfg().get("disk_cache", True)) and disk_limit() > 0


def _disk_dir() -> str:
    from .config import DATA_DIR
    return str(DATA_DIR / "stream_cache")


def _disk_path(url: str) -> str:
    """分片落盘路径：``sha1(scheme://host/path)`` + 原后缀。

    故意丢掉查询串：播放地址里的签名每次都会变，带着它当键就永远命中不了；
    同一条路径的分片内容是稳定的，这才是「回看 / 重启后再看」能命中的前提。
    """
    p = urlsplit(str(url or ""))
    path = p.path or ""
    ext = os.path.splitext(path)[1].lower()
    if ext not in _MEDIA_BY_EXT:
        ext = ".ts"
    raw = "%s://%s%s" % (p.scheme, p.netloc, path)
    return os.path.join(_disk_dir(), hashlib.sha1(raw.encode("utf-8")).hexdigest() + ext)


def _disk_scan_locked() -> int:
    """算一次目录占用，并把文件名装进 ``_DISK_NAMES``（只需在进程生命周期内做一次）。"""
    global _DISK_BYTES
    if _DISK_BYTES is not None:
        return _DISK_BYTES
    total = 0
    root = _disk_dir()
    try:
        os.makedirs(root, exist_ok=True)
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if not entry.is_file() or entry.name.endswith(".part"):
                        continue
                    total += entry.stat().st_size
                    _DISK_NAMES.add(entry.name)
                except OSError:
                    pass
    except OSError:
        pass
    _DISK_BYTES = total
    return total


def _disk_read(url: str) -> Optional[Tuple[bytes, str]]:
    if not disk_enabled():
        return None
    path = _disk_path(url)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if not data:
        return None
    try:
        os.utime(path, None)          # 触摸一下：淘汰按最久未访问
    except OSError:
        pass
    with _DISK_LOCK:
        _DISK_NAMES.add(os.path.basename(path))
    media = _MEDIA_BY_EXT.get(os.path.splitext(path)[1].lower(), "video/mp2t")
    return data, media


def _disk_write(url: str, data: bytes, media: str) -> None:
    """写盘后按上限淘汰；任何 IO 失败都静默放过（缓存不是必须的）。"""
    global _DISK_BYTES
    if not disk_enabled():
        return
    path = _disk_path(url)
    tmp = path + ".part"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return
    with _DISK_LOCK:
        used = _disk_scan_locked()
        name = os.path.basename(path)
        if name not in _DISK_NAMES:
            _DISK_BYTES = used + len(data)
        _DISK_NAMES.add(name)
    _disk_evict()


def _disk_evict() -> None:
    """超过上限时按 mtime 从旧到新删到 80%（留出缓冲，不必每写一段都删）。"""
    global _DISK_BYTES
    limit = disk_limit()
    if limit <= 0:
        return
    with _DISK_LOCK:
        used = _disk_scan_locked()
        if used <= limit:
            return
        target = int(limit * 0.8)
        entries = []
        try:
            with os.scandir(_disk_dir()) as it:
                for entry in it:
                    try:
                        if entry.is_file() and not entry.name.endswith(".part"):
                            st = entry.stat()
                            entries.append((st.st_mtime, entry.name, st.st_size))
                    except OSError:
                        pass
        except OSError:
            return
        entries.sort()                 # mtime 升序：最久没碰的排前面
        for _, name, size in entries:
            if used <= target:
                break
            try:
                os.remove(os.path.join(_disk_dir(), name))
                used -= size
                _DISK_NAMES.discard(name)
            except OSError:
                pass
        _DISK_BYTES = max(0, used)


def _memory_put(url: str, data: bytes, media: str) -> None:
    """只进内存层：磁盘命中回填用，避免「读盘 → 又写一遍盘」。"""
    global _BYTES
    limit = _limit_bytes()
    if limit <= 0:
        return
    expire_at = time.time() + int((_stream_cfg().get("cache_ttl") or 300))
    with _LOCK:
        if url in _CACHE:
            _drop_locked(url)
        _CACHE[url] = (data, media or "application/octet-stream", expire_at)
        _BYTES += len(data)
        while _BYTES > limit and _CACHE:
            old = next(iter(_CACHE))
            if old == url:               # 当前项比整个上限还大：撤掉自己
                _drop_locked(old)
                break
            _drop_locked(old)


def get(url: str) -> Optional[Tuple[bytes, str]]:
    """命中返回 (bytes, media_type)；内存未命中再看磁盘，命中后回填内存。"""
    if not enabled():
        return None
    now = time.time()
    with _LOCK:
        item = _CACHE.get(url)
        if item:
            data, media, expire_at = item
            if expire_at < now:
                _drop_locked(url)
            else:
                _CACHE.move_to_end(url)
                return data, media
    hit = _disk_read(url)
    if not hit:
        return None
    _memory_put(url, hit[0], hit[1])
    return hit


def put(url: str, data: bytes, media: str) -> None:
    """写入内存 + 磁盘；空数据 / 超大分片直接忽略。"""
    if not data or len(data) > MAX_ITEM or not enabled():
        return
    _memory_put(url, data, media)
    try:
        _disk_write(url, data, media)
    except Exception:                 # 磁盘满 / 没权限：不影响播放
        pass


def _drop_locked(url: str) -> None:
    global _BYTES
    item = _CACHE.pop(url, None)
    if item:
        _BYTES -= len(item[0])
    if _BYTES < 0:
        _BYTES = 0


def note_playlist(movie_id: int, text: str, base_url: str) -> None:
    """记住播放列表里的分片顺序，供「取到第 i 段 → 预取 i+1、i+2」使用。

    只收非注释行（分片 / 子列表 URI）；子列表地址不是分片，后面预取时会被过滤。
    顺带把每段前的 ``#EXTINF:<秒>`` 记下来：进度条要显示「已缓存到第几分钟」，
    只有段序号是不够的，得有每段时长才能换算成片内时间。
    """
    if not movie_id or not text:
        return
    base = urldefrag(base_url or "")[0]
    urls: List[str] = []
    durs: List[float] = []
    pending = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF:"):
            try:
                pending = float(line[8:].split(",")[0].strip())
            except (TypeError, ValueError):
                pending = 0.0
            continue
        if line.startswith("#"):
            continue
        try:
            url = urljoin(base, line)
        except ValueError:
            continue
        if url.lower().split("?")[0].endswith(".m3u8"):
            continue                    # 主列表的变体地址：不是分片
        urls.append(url)
        durs.append(max(0.0, pending))
        pending = 0.0
    if not urls:
        return
    with _LOCK:
        _ORDER[int(movie_id)] = (urls, time.time())
        _DUR[int(movie_id)] = durs


def note_request(movie_id: int, url: str, headers: Dict[str, str],
                 proxies: Any = None) -> None:
    """记录一次分片请求，并把后面几段排进后台预取（重复排队会去重）。

    预读深度与并发都受配置控制：
    * ``stream.prefetch_count``：往后看几段（默认 4，0 = 关闭预取）；
    * ``stream.prefetch_workers``：同时最多几个预取任务（默认 2）。

    并发上限是刻意保留的：代理出口带宽总量有限，预取开太多会抢走当前播放
    那一路的带宽，反而更卡。到达上限时这一轮就不排新任务，等下一次请求再补。
    """
    cfg = _stream_cfg()
    if not enabled() or not movie_id:
        return
    try:
        depth = int(cfg.get("prefetch_count", 4))
        workers = int(cfg.get("prefetch_workers", 2))
    except (TypeError, ValueError):
        depth, workers = 4, 2
    if depth <= 0 or workers <= 0:
        return
    now = time.time()
    with _LOCK:
        item = _ORDER.get(int(movie_id))
        if not item or now - item[1] > _ORDER_TTL:
            return
        urls = item[0]
    try:
        idx = urls.index(url)
    except ValueError:
        return
    with _LOCK:
        _LAST_IDX[int(movie_id)] = idx
    _schedule(movie_id, urls, idx + 1, headers, proxies)


def _schedule(movie_id: int, urls: List[str], start: int,
              headers: Dict[str, str], proxies: Any) -> None:
    """从 ``start`` 开始补预取，但不超过「最后请求的那一段 + 预读深度」。

    预取任务完成时会再调一次这里，形成滚动预读：冷启动时能把 2 路并发一直
    填满到深度上限，播放推进后又只盯着当前进度往前的窗口，不会把整部片子
    都下载下来（代理流量很贵）。
    """
    cfg = _stream_cfg()
    try:
        depth = int(cfg.get("prefetch_count", 4))
        workers = int(cfg.get("prefetch_workers", 2))
    except (TypeError, ValueError):
        depth, workers = 4, 2
    if depth <= 0 or workers <= 0:
        return
    global _ACTIVE
    with _LOCK:
        item = _ORDER.get(int(movie_id))
        if not item:
            return
        urls = item[0]
        last = _LAST_IDX.get(int(movie_id), start)
        end = min(len(urls), max(start, last + 1 + depth))
        slots = max(0, workers - _ACTIVE)
        todo = []
        for i in range(start, end):
            if len(todo) >= slots:
                break
            u = urls[i]
            if u in _CACHE or u in _PENDING:
                continue
            _PENDING.add(u)
            todo.append(u)
        if not todo:
            return
        _ACTIVE += len(todo)
    from .threadpool import submit_task
    for u in todo:
        try:
            submit_task(_prefetch_one, int(movie_id), u, headers, proxies)
        except Exception:
            with _LOCK:
                _PENDING.discard(u)
                _ACTIVE = max(0, _ACTIVE - 1)


def _prefetch_one(movie_id: int, url: str, headers: Dict[str, str],
                  proxies: Any) -> None:
    """后台取一段：成功就入缓存，失败静默（不重试、不报错、不影响播放）。"""
    global _ACTIVE
    try:
        from .http_pool import session
        r = session().get(url, headers=headers, timeout=(6, 12), proxies=proxies)
        if r.status_code >= 400:
            return
        body = r.content or b""
        media = r.headers.get("Content-Type") or ""
        if not cacheable(url, media):
            # 伪装扩展名（.css / .svg）的分片：按首字节判断是否 MPEG-TS
            if body[:1] != b"\x47":
                return
            media = "video/mp2t"
        put(url, body, media)
    except Exception:
        return
    finally:
        with _LOCK:
            _PENDING.discard(url)
            _ACTIVE = max(0, _ACTIVE - 1)
        # 滚动预读：这一段取完立刻补下一段（窗口仍受「最后请求 + 深度」限制）
        try:
            with _LOCK:
                item = _ORDER.get(int(movie_id))
            if item:
                try:
                    idx = item[0].index(url)
                except ValueError:
                    idx = -1
                if idx >= 0:
                    _schedule(movie_id, item[0], idx + 1, headers, proxies)
        except Exception:
            pass


def progress(movie_id: int) -> Dict[str, Any]:
    """已缓存进度：把「哪几段在内存里」换算成片内时间区间（秒）。

    播放器进度条据此画出「已缓存」这一段（回看过 / 预取过 / 正在播都算）。
    没有播放列表记录（还没开始播 / 已过期）时返回空，前端不画即可。
    """
    empty = {"total": 0.0, "ranges": [], "cached": 0, "segments": 0}
    if not movie_id:
        return empty
    now = time.time()
    use_disk = disk_enabled()
    with _DISK_LOCK:
        disk_names = set(_DISK_NAMES) if use_disk else set()
    with _LOCK:
        item = _ORDER.get(int(movie_id))
        if not item or now - item[1] > _ORDER_TTL:
            return empty
        urls = item[0]
        durs = _DUR.get(int(movie_id)) or []
        cursor = 0.0
        start = None
        ranges: List[List[float]] = []
        cached = 0
        for i, u in enumerate(urls):
            dur = float(durs[i]) if i < len(durs) else 0.0
            hit = u in _CACHE
            if not hit and use_disk:
                hit = os.path.basename(_disk_path(u)) in disk_names
            if hit:
                cached += 1
                if start is None:
                    start = cursor
            elif start is not None:
                ranges.append([start, cursor])
                start = None
            cursor += dur
        if start is not None:
            ranges.append([start, cursor])
        total = cursor
    return {"total": round(total, 3),
            "ranges": [[round(a, 3), round(b, 3)] for a, b in ranges],
            "cached": cached, "segments": len(urls)}


def stats() -> Dict[str, Any]:
    """调试 / 设置页用：内存与磁盘两层的规模与占用。"""
    with _DISK_LOCK:
        used = _disk_scan_locked() if disk_enabled() else 0
        files = len(_DISK_NAMES) if disk_enabled() else 0
    with _LOCK:
        return {"items": len(_CACHE), "bytes": _BYTES, "limit": _limit_bytes(),
                "movies": len(_ORDER), "active": _ACTIVE, "pending": len(_PENDING),
                "disk_on": disk_enabled(), "disk_files": files,
                "disk_bytes": used, "disk_limit": disk_limit()}


def clear() -> int:
    """清空内存与磁盘两层；返回删掉的磁盘文件数（设置页给个「确实清了」的回执）。"""
    global _BYTES, _ACTIVE, _DISK_BYTES, _DISK_NAMES
    with _LOCK:
        _CACHE.clear()
        _ORDER.clear()
        _DUR.clear()
        _LAST_IDX.clear()
        _PENDING.clear()
        _BYTES = 0
        _ACTIVE = 0
    # 磁盘层一起清：用户点「清空缓存」就是不想它占地方了
    removed = 0
    with _DISK_LOCK:
        root = _disk_dir()
        try:
            with os.scandir(root) as it:
                for entry in it:
                    try:
                        if entry.is_file():
                            os.remove(entry.path)
                            removed += 1
                    except OSError:
                        pass
        except OSError:
            pass
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            pass
        _DISK_NAMES = set()
        _DISK_BYTES = 0
    return removed
