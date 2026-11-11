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
import shutil
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
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
# 磁盘布局版本：v2 起按「影片 / 来源 / 段号桶」分层，旧版平铺文件在首次扫描时丢弃
_LAYOUT_VERSION = "2"
_LAYOUT_MARK = ".layout"
# 每个桶放多少段：单目录文件数上限（200 段约 200 MB 量级，浏览 / 删除都不卡）
_BUCKET_SIZE = 200

_LOCK = threading.Lock()
# url -> (bytes, media_type, expire_at)
_CACHE: "OrderedDict[str, Tuple[bytes, str, float]]" = OrderedDict()
_BYTES = 0
# movie_id -> (分片绝对地址列表, 记录时间)
_ORDER: Dict[int, Tuple[List[str], float]] = {}
# movie_id -> 每段时长（秒），与 _ORDER 的下标一一对应；用于把「已缓存第几段」换算成时间
_DUR: Dict[int, List[float]] = {}
# 分片地址 -> (movie_id, 来源目录名, 段号)：落盘路径靠它按「影片 / 来源 / 桶」分层。
# 地址里的查询串会变，但同一路径分片内容稳定，所以这里记的是当前播放列表的绝对地址。
_SEG_PATH: Dict[str, Tuple[int, str, int]] = {}
# movie_id -> 最近一次真正被请求的分片下标（滚动预读的窗口基准）
_LAST_IDX: Dict[int, int] = {}
# 正在预取的分片，避免同一段被重复排队
_PENDING: set = set()
# 正在跑的预取任务数：并发上限（stream.prefetch_workers）由它守住
_ACTIVE = 0
# 预取专用线程池：不复用短任务池（4 线程）——预取是持续的后台下载，
# 一开就把探测 / 抽帧 / 组件下载挤在队尾。按配置动态调整大小。
_PREFETCH_POOL: Optional[ThreadPoolExecutor] = None
_PREFETCH_SIZE = 0

# ---- 空闲预取（暂停时往后续下）----
# 和播放中的预取完全分开：独立线程池、独立取消标志、独立字节上限。
# 触发不靠猜：前端暂停时显式调用 start_idle()，恢复播放 / 关闭播放器调
# stop_idle()；服务端收到真实分片请求时也会自动取消（说明播放器又在拉了）。
_IDLE: Dict[int, Dict[str, Any]] = {}
_IDLE_POOL: Optional[ThreadPoolExecutor] = None
_IDLE_POOL_SIZE = 0
# 暂停瞬间还有几个在途分片请求：这几秒内的真实请求不算「恢复播放」
_IDLE_GRACE = 8.0
# 硬超时：源站极慢时避免一个暂停任务挂半小时（按内容秒数控制的是窗口，不是耗时）
_IDLE_MAX_WALL = 15 * 60

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


def _stable_url_key(url: str) -> str:
    """分片内容的稳定身份：``scheme://host/path``（丢掉会变的查询串 / 签名）。"""
    p = urlsplit(str(url or ""))
    return "%s://%s%s" % (p.scheme, p.netloc, p.path or "")


def _safe_key(raw: Any, fallback: str) -> str:
    """目录名只留安全字符：来源 id 正常是数字，异常值兜底成主机名 / ``src``。"""
    s = "".join(ch for ch in str(raw or "") if ch.isalnum() or ch in "-_")[:32]
    return s or fallback


def _disk_path(url: str) -> str:
    """分片落盘路径：``<movie_id>/<来源>/<桶>/<段号>-<hash>.<ext>``。

    * 影片 / 来源 / 段号来自播放列表，和签名地址无关，重播时路径不变；
    * 每 ``_BUCKET_SIZE`` 段一个子目录，单目录文件数有上限；
    * 文件名里带 ``sha1(scheme://host/path)`` 前 8 位：同一部片的多个清晰度 /
      备用源即使段号相同也不会互相覆盖，读回来的永远是这条地址的内容；
    * 不在任何播放列表里的（外挂字幕等）放 ``_misc/<hash 前 2 位>/``，同样分桶。
    """
    p = urlsplit(str(url or ""))
    ext = os.path.splitext(p.path or "")[1].lower()
    if ext not in _MEDIA_BY_EXT:
        ext = ".ts"
    digest = hashlib.sha1(_stable_url_key(url).encode("utf-8")).hexdigest()
    info = _SEG_PATH.get(str(url or ""))
    if info:
        movie_id, source_key, idx = info
        name = "%05d-%s%s" % (idx, digest[:8], ext)
        return os.path.join(_disk_dir(), str(movie_id), source_key,
                            "%04d" % (idx // _BUCKET_SIZE), name)
    return os.path.join(_disk_dir(), "_misc", digest[:2], digest + ext)


def _disk_rel(url: str) -> str:
    """相对 ``data/stream_cache`` 的路径（统一用 /）：``_DISK_NAMES`` 里存的就是它。"""
    root = _disk_dir()
    return os.path.relpath(_disk_path(url), root).replace(os.sep, "/")


def _migrate_layout_locked(root: str) -> None:
    """一次性把旧版「全部平铺在根目录」的文件清掉，只保留一套布局。

    缓存是可重建的：旧文件按 40 位 sha1 存放在根目录，新布局全部在子目录里，
    两套同时存在只会让「这段到底在不在」的判断出现两套真源。直接丢弃，
    用户重看时重新缓存即可（当前正在播的分片在内存层，不受影响）。
    """
    mark = os.path.join(root, _LAYOUT_MARK)
    try:
        with open(mark, "r", encoding="ascii") as f:
            if f.read().strip() == _LAYOUT_VERSION:
                return
    except OSError:
        pass
    try:
        with os.scandir(root) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                name = entry.name
                # 旧布局：40 位 sha1 + 后缀；顺带清掉上次中断留下的 .part
                legacy = len(name) > 40 and all(c in "0123456789abcdef" for c in name[:40])
                if legacy or name.endswith(".part"):
                    try:
                        os.remove(entry.path)
                    except OSError:
                        pass
    except OSError:
        pass
    try:
        with open(mark, "w", encoding="ascii") as f:
            f.write(_LAYOUT_VERSION)
    except OSError:
        pass


def _disk_scan_locked() -> int:
    """算一次目录占用，并把相对路径装进 ``_DISK_NAMES``（进程内只需做一次）。"""
    global _DISK_BYTES
    if _DISK_BYTES is not None:
        return _DISK_BYTES
    root = _disk_dir()
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        _DISK_BYTES = 0
        return 0
    _migrate_layout_locked(root)
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if name.startswith(".") or name.endswith(".part"):
                continue
            full = os.path.join(dirpath, name)
            try:
                total += os.path.getsize(full)
                _DISK_NAMES.add(os.path.relpath(full, root).replace(os.sep, "/"))
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
        _DISK_NAMES.add(_disk_rel(url))
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
        rel = os.path.relpath(path, _disk_dir()).replace(os.sep, "/")
        if rel not in _DISK_NAMES:
            _DISK_BYTES = used + len(data)
        _DISK_NAMES.add(rel)
    _disk_evict()


def _disk_evict() -> None:
    """超过上限时按 mtime 从旧到新删到 80%（留出缓冲，不必每写一段都删）。"""
    global _DISK_BYTES
    limit = disk_limit()
    if limit <= 0:
        return
    root = _disk_dir()
    with _DISK_LOCK:
        used = _disk_scan_locked()
        if used <= limit:
            return
        target = int(limit * 0.8)
        entries = []
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                if name.startswith(".") or name.endswith(".part"):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                entries.append((st.st_mtime, rel, st.st_size))
        entries.sort()                 # mtime 升序：最久没碰的排前面
        removed = False
        for _, rel, size in entries:
            if used <= target:
                break
            try:
                os.remove(os.path.join(root, rel.replace("/", os.sep)))
                used -= size
                _DISK_NAMES.discard(rel)
                removed = True
            except OSError:
                pass
        _DISK_BYTES = max(0, used)
        if removed:
            # 顺手收掉空目录：删空后留下成千上万个空桶也很烦
            for dirpath, dirnames, filenames in os.walk(root, topdown=False):
                if dirpath != root and not dirnames and not filenames:
                    try:
                        os.rmdir(dirpath)
                    except OSError:
                        pass


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


def note_playlist(movie_id: int, text: str, base_url: str,
                  source_key: str = "") -> None:
    """记住播放列表里的分片顺序，供「取到第 i 段 → 预取 i+1、i+2」使用。

    只收非注释行（分片 / 子列表 URI）；子列表地址不是分片，后面预取时会被过滤。
    顺带把每段前的 ``#EXTINF:<秒>`` 记下来：进度条要显示「已缓存到第几分钟」，
    只有段序号是不够的，得有每段时长才能换算成片内时间。

    ``source_key``（来源 id）只用于磁盘目录：``<movie_id>/<来源>/<桶>/``。
    缺省时退回主机名，保证路径照样稳定、不用带签名。
    """
    if not movie_id or not text:
        return
    base = urldefrag(base_url or "")[0]
    src = _safe_key(source_key, _safe_key(urlsplit(base).hostname, "src"))
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
        prev = _ORDER.get(int(movie_id))
        if prev:
            for old in prev[0]:
                _SEG_PATH.pop(old, None)
        _ORDER[int(movie_id)] = (urls, time.time())
        _DUR[int(movie_id)] = durs
        for i, u in enumerate(urls):
            _SEG_PATH[u] = (int(movie_id), src, i)


def note_request(movie_id: int, url: str, headers: Dict[str, str],
                 proxies: Any = None) -> None:
    """记录一次分片请求，并把后面一段窗口排进后台预取（重复排队会去重）。

    预读窗口与并发都受配置控制：
    * ``stream.prefetch_seconds``：往前预取约多少秒（默认 60，0 = 关闭预取）；
    * ``stream.prefetch_count``：最多预取多少段（默认 20，防止超短分片的片子
      一次排上千个任务）；
    * ``stream.prefetch_workers``：同时最多几个预取任务（默认 4）。

    为什么按秒而不是按段：分片时长从 2 秒到 10 秒都有，按固定段数算，
    短分片的片子缓存不住 60 秒，长分片的片子又会一口气拉几百 MB。
    并发是刻意设上限的：代理出口带宽有限，开太多会抢走当前播放那一路。
    """
    cfg = _stream_cfg()
    if not enabled() or not movie_id:
        return
    try:
        workers = int(cfg.get("prefetch_workers", 4))
    except (TypeError, ValueError):
        workers = 4
    if _prefetch_window_size(cfg) <= 0 or workers <= 0:
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
        # 播放器又开始拉真实分片了：暂停时的空闲预取让位，别跟播放抢带宽。
        # 宽限几秒是因为暂停瞬间还有在途请求，不能刚排上就自己取消。
        idle = _IDLE.get(int(movie_id))
        if (idle and idle.get("running")
                and now - float(idle.get("started_at") or 0) > _IDLE_GRACE):
            idle["cancel"] = True
            _idle_finish(idle, "playing")
    _schedule(movie_id, urls, idx + 1, headers, proxies)


def _prefetch_window_size(cfg: Dict[str, Any]) -> float:
    try:
        return max(0.0, float(cfg.get("prefetch_seconds", 60)))
    except (TypeError, ValueError):
        return 60.0


def _prefetch_end(urls: List[str], durs: List[float], start: int,
                  cfg: Dict[str, Any]) -> int:
    """从 ``start`` 往后数到「约 prefetch_seconds 秒」，并受段数上限约束。"""
    try:
        max_seg = max(1, int(cfg.get("prefetch_count", 20)))
    except (TypeError, ValueError):
        max_seg = 20
    return _window_end(urls, durs, start, _prefetch_window_size(cfg), max_seg)


def _window_end(urls: List[str], durs: List[float], start: int,
                want: float, max_seg: int) -> int:
    """从 ``start`` 起按分片时长往后累加，凑够 ``want`` 秒（至少 3 段）就停。"""
    end = start
    acc = 0.0
    while end < len(urls) and end - start < max_seg:
        acc += float(durs[end]) if end < len(durs) else 0.0
        end += 1
        if acc >= want and end - start >= 3:
            break
    return end


def _index_at(durs: List[float], position: float) -> int:
    """把播放位置（秒）换算成分片下标；超出总时长时取最后一段。"""
    if not durs:
        return 0
    acc = 0.0
    for i, dur in enumerate(durs):
        acc += max(0.0, float(dur))
        if position < acc:
            return i
    return len(durs) - 1


def _idle_cfg(cfg: Dict[str, Any]) -> Tuple[bool, float, int, int]:
    """空闲预取配置：开关 / 内容秒数 / 并发 / 单次字节上限（MB）。"""
    try:
        seconds = max(0.0, float(cfg.get("idle_prefetch_seconds", 300)))
    except (TypeError, ValueError):
        seconds = 300.0
    try:
        workers = max(1, int(cfg.get("idle_prefetch_workers", 2)))
    except (TypeError, ValueError):
        workers = 2
    try:
        mb = max(0, int(cfg.get("idle_prefetch_mb", 512)))
    except (TypeError, ValueError):
        mb = 512
    return bool(cfg.get("idle_prefetch", True)), seconds, workers, mb


def _idle_pool(workers: int) -> ThreadPoolExecutor:
    global _IDLE_POOL, _IDLE_POOL_SIZE
    with _LOCK:
        if _IDLE_POOL is None or _IDLE_POOL_SIZE != workers:
            _IDLE_POOL = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="avm-idle")
            _IDLE_POOL_SIZE = workers
        return _IDLE_POOL


def _idle_view(st: Dict[str, Any]) -> Dict[str, Any]:
    """对外状态（不含请求头 / 代理这类不该回给前端的字段）。"""
    keys = ("running", "done", "total", "seconds", "want", "bytes", "byte_limit",
            "ended", "message", "position", "source_id", "fails")
    return {k: st.get(k) for k in keys}


def _idle_finish(st: Dict[str, Any], reason: str) -> None:
    """结束一个空闲预取（调用方持 _LOCK）。"""
    st["running"] = False
    st["ended"] = reason


def _idle_disk_names() -> Set[str]:
    """磁盘已有文件名快照：判断「这段是否已缓存」不用逐段 stat。"""
    if not disk_enabled():
        return set()
    with _DISK_LOCK:
        _disk_scan_locked()
        return set(_DISK_NAMES)


def _cached_ahead_locked(urls: List[str], durs: List[float], start: int,
                         names: Set[str]) -> float:
    """从 start 起连续已缓存的分片能覆盖多少秒（调用方持 _LOCK）。"""
    acc = 0.0
    for i in range(start, len(urls)):
        u = urls[i]
        if u not in _CACHE and _disk_rel(u) not in names:
            break
        acc += float(durs[i]) if i < len(durs) else 0.0
    return round(acc, 1)


def start_idle(movie_id: int, position: float = 0.0, source_id: str = "",
               headers: Optional[Dict[str, str]] = None,
               proxies: Any = None) -> Dict[str, Any]:
    """暂停后从 ``position`` 往后预下约 ``idle_prefetch_seconds`` 秒。

    只排任务、立即返回；真正下载在独立线程池里。同一部片重复调用（比如
    前端每 3 秒查状态时误触）不会重复排队：起点与终点都没变就直接返回。
    """
    mid = int(movie_id or 0)
    cfg = _stream_cfg()
    on, want, workers, mb = _idle_cfg(cfg)
    if not mid or not on or want <= 0 or not enabled() or not disk_enabled():
        return idle_status(mid)
    names = _idle_disk_names()
    now = time.time()
    with _LOCK:
        item = _ORDER.get(mid)
        if not item or now - item[1] > _ORDER_TTL:
            return {"enabled": bool(on), "running": False, "ended": "no-playlist",
                    "done": 0, "total": 0, "seconds": 0.0, "want": want,
                    "bytes": 0, "byte_limit": mb * 1024 * 1024, "message": "",
                    "position": position, "source_id": source_id, "fails": 0}
        urls = item[0]
        durs = _DUR.get(mid) or []
        start = _index_at(durs, float(position or 0.0))
        end = _window_end(urls, durs, start, want, max(1, len(urls)))
        prev = _IDLE.get(mid)
        if prev and prev.get("running"):
            if prev.get("start") == start and prev.get("end") == end:
                return _idle_view(prev)
            # 换了个位置重新暂停：旧任务让位（在途请求会自然结束）
            prev["cancel"] = True
            _idle_finish(prev, "restart")
        _IDLE[mid] = {
            "running": True, "cancel": False, "start": start, "end": end,
            "next": start, "done": 0, "total": max(0, end - start),
            "active": 0, "bytes": 0, "byte_limit": mb * 1024 * 1024,
            "workers": workers, "want": want,
            "position": float(position or 0.0), "source_id": source_id or "",
            "started_at": now, "ended": "", "message": "", "fails": 0,
            "headers": headers or {}, "proxies": proxies,
        }
        st = _IDLE[mid]
        st["seconds"] = _cached_ahead_locked(urls, durs, start, names)
    _idle_schedule(mid)
    with _LOCK:
        return _idle_view(_IDLE.get(mid) or st)


def stop_idle(movie_id: int, reason: str = "cancelled") -> Dict[str, Any]:
    """停止某部片的空闲预取（恢复播放 / 关闭播放器 / 切换来源时调用）。"""
    mid = int(movie_id or 0)
    with _LOCK:
        st = _IDLE.get(mid)
        if not st:
            return {"running": False, "ended": reason}
        if st.get("running"):
            # 只置标志：在途请求由 requests 自然结束，不强行中断连接
            st["cancel"] = True
            _idle_finish(st, reason)
        return _idle_view(st)


def idle_status(movie_id: int) -> Dict[str, Any]:
    """空闲预取状态：前端轮询它显示「后台预下 x/y」。"""
    cfg = _stream_cfg()
    on, want, workers, mb = _idle_cfg(cfg)
    with _LOCK:
        st = _IDLE.get(int(movie_id or 0))
        view = _idle_view(st) if st else {"running": False, "done": 0, "total": 0,
                                          "seconds": 0.0, "bytes": 0, "ended": "",
                                          "message": "", "fails": 0,
                                          "position": 0.0, "source_id": ""}
    view.update({"enabled": bool(on) and want > 0,
                 "want": want, "workers": workers,
                 "byte_limit": mb * 1024 * 1024})
    if (disk_enabled() and (view.get("running") or view.get("ended") == "done")):
        # 已缓存秒数随磁盘增长，前端不必自己算
        names = _idle_disk_names()
        with _LOCK:
            item = _ORDER.get(int(movie_id or 0))
            st = _IDLE.get(int(movie_id or 0))
            if item and st:
                view["seconds"] = _cached_ahead_locked(
                    item[0], _DUR.get(int(movie_id or 0)) or [],
                    st.get("start") or 0, names)
    return view


def _idle_schedule(movie_id: int) -> None:
    """把窗口内的后续分片排进空闲预取池（并发由 idle_prefetch_workers 守住）。"""
    names = _idle_disk_names()
    with _LOCK:
        st = _IDLE.get(int(movie_id))
        if not st or not st.get("running") or st.get("cancel"):
            return
        item = _ORDER.get(int(movie_id))
        if not item:
            _idle_finish(st, "no-playlist")
            return
        if time.time() - float(st.get("started_at") or 0) > _IDLE_MAX_WALL:
            _idle_finish(st, "timeout")
            return
        urls = item[0]
        durs = _DUR.get(int(movie_id)) or []
        # 播放列表可能被换过一次（同一部片重新起播），窗口终点按当前列表长度收一下
        st["end"] = min(int(st.get("end") or 0), len(urls))
        limit = int(st.get("byte_limit") or 0)
        if limit and st.get("bytes", 0) >= limit:
            _idle_finish(st, "limit")
            return
        todo = []
        while st["active"] < st["workers"] and st["next"] < st["end"]:
            u = urls[st["next"]]
            st["next"] += 1
            if u in _CACHE or _disk_rel(u) in names:
                st["done"] += 1       # 之前就缓存过：直接算进度，不重复下载
                continue
            st["active"] += 1
            todo.append(u)
        st["seconds"] = _cached_ahead_locked(urls, durs, st["start"], names)
        if not todo:
            if st["active"] == 0 and st["next"] >= st["end"]:
                _idle_finish(st, "done")
            return
        headers = st.get("headers") or {}
        proxies = st.get("proxies")
        workers = st["workers"]
    pool = _idle_pool(workers)
    for u in todo:
        try:
            pool.submit(_idle_one, int(movie_id), u, headers, proxies)
        except Exception:
            with _LOCK:
                st["active"] = max(0, st["active"] - 1)
                st["done"] += 1


def _idle_one(movie_id: int, url: str, headers: Dict[str, str],
              proxies: Any) -> None:
    """空闲预取取一段：成功入缓存，失败静默；403/410 连续两次就停。"""
    status = 0
    body = b""
    try:
        from .http_pool import session
        r = session().get(url, headers=headers, timeout=(8, 30), proxies=proxies)
        status = int(r.status_code)
        if status < 400:
            body = r.content or b""
            media = r.headers.get("Content-Type") or ""
            if not cacheable(url, media):
                if body[:1] != b"\x47":
                    body = b""
                else:
                    media = "video/mp2t"
            if body:
                put(url, body, media)
    except Exception:
        body = b""
    finally:
        with _LOCK:
            st = _IDLE.get(int(movie_id))
            if st:
                st["active"] = max(0, st["active"] - 1)
                st["done"] += 1
                st["bytes"] = st.get("bytes", 0) + len(body)
                if status in (403, 410):
                    st["fails"] = st.get("fails", 0) + 1
                    if st["fails"] >= 2:
                        # 签名过期：停下等下次重新起播（不重试、不刷日志）
                        st["message"] = "源站返回 HTTP %d，已停止预下" % status
                        _idle_finish(st, "failed")
                limit = int(st.get("byte_limit") or 0)
                if limit and st["bytes"] >= limit:
                    _idle_finish(st, "limit")
        _idle_schedule(movie_id)


def _prefetch_pool(workers: int) -> ThreadPoolExecutor:
    global _PREFETCH_POOL, _PREFETCH_SIZE
    with _LOCK:
        if _PREFETCH_POOL is None or _PREFETCH_SIZE != workers:
            _PREFETCH_POOL = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="avm-prefetch")
            _PREFETCH_SIZE = workers
        return _PREFETCH_POOL


def _schedule(movie_id: int, urls: List[str], start: int,
              headers: Dict[str, str], proxies: Any) -> None:
    """从 ``start`` 开始补预取，但不超过「最后请求的那一段 + 预读窗口」。

    预取任务完成时会再调一次这里，形成滚动预读：冷启动时能把并发一直
    填满到窗口上限，播放推进后又只盯着当前进度往前的窗口，不会把整部片子
    都下载下来（代理流量很贵）。
    """
    cfg = _stream_cfg()
    try:
        workers = int(cfg.get("prefetch_workers", 4))
    except (TypeError, ValueError):
        workers = 4
    if _prefetch_window_size(cfg) <= 0 or workers <= 0:
        return
    global _ACTIVE
    with _LOCK:
        item = _ORDER.get(int(movie_id))
        if not item:
            return
        urls = item[0]
        durs = _DUR.get(int(movie_id)) or []
        last = _LAST_IDX.get(int(movie_id), start)
        # 窗口终点只按「播放器最后请求的那一段」算：预取任务完成后的滚动
        # 预读只能把窗口填满，不能把窗口本身往前推——否则会一路把整部片
        # 下载下来（实测 30 秒就多拉了 3 分钟的缓存）。
        cap = _prefetch_end(urls, durs, last + 1, cfg)
        base = max(start, last + 1)
        if base >= cap:
            return
        slots = max(0, workers - _ACTIVE)
        todo = []
        for i in range(base, cap):
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
    pool = _prefetch_pool(workers)
    for u in todo:
        try:
            pool.submit(_prefetch_one, int(movie_id), u, headers, proxies)
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
        # 读超时放宽到 30 秒：慢源单个分片 1–2 MB、只有 100–200 KB/s 时，
        # 12 秒会拦腰截断，表现为「预取一直失败，缓冲永远上不去」。
        r = session().get(url, headers=headers, timeout=(8, 30), proxies=proxies)
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
                hit = _disk_rel(u) in disk_names
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
        _SEG_PATH.clear()
        _LAST_IDX.clear()
        _PENDING.clear()
        for st in _IDLE.values():
            st["cancel"] = True
            st["running"] = False
            st["ended"] = "cleared"
        _IDLE.clear()
        _BYTES = 0
        _ACTIVE = 0
    # 磁盘层一起清：用户点「清空缓存」就是不想它占地方了
    removed = 0
    with _DISK_LOCK:
        root = _disk_dir()
        try:
            for _, _, filenames in os.walk(root):
                removed += len(filenames)
        except OSError:
            pass
        try:
            shutil.rmtree(root)
        except OSError:
            pass
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            pass
        _DISK_NAMES = set()
        _DISK_BYTES = 0
    return removed
