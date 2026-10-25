# -*- coding: utf-8 -*-
"""浏览器播放兼容层：把「视频编码浏览器可解、但容器/音轨不兼容」的影片快速
重封装（remux）为 MP4，供网页内直接播放。

只做重封装，不做重编码：
- 视频流 ``-c:v copy`` 直接复制 → 几乎零 CPU，耗时只受磁盘 IO 限制；
- 音轨转 AAC → 兼容浏览器（MKV 常见的 AC3 / DTS / FLAC 浏览器都不认）；
- 内嵌图形字幕（PGS / VOBSUB）无法放进 MP4，会被舍弃（推荐用外挂字幕）。

若视频编码本身浏览器无法解码（HEVC、10bit H.264 等），本模块**不处理**——
这类要重编码或硬解，交给系统播放器，避免让弱 CPU 的 NAS 陷入长转码。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import REMUX_DIR

# 浏览器可直接播放的容器（按**文件扩展名**判断）。
# 注意：MKV 与 WebM 的 ffprobe format_name 都是 "matroska,webm"，
# 若按容器名判断会把 MKV 误判成可播，所以这里只看扩展名。
FRIENDLY_EXTS = {"mp4", "m4v", "mov", "webm"}
FRIENDLY_VCODECS = {"h264", "vp8", "vp9", "av1"}
FRIENDLY_ACODECS = {"aac", "mp3", "opus", "vorbis"}

_LOCK = threading.Lock()
_STATE: Dict[int, Dict[str, Any]] = {}          # movie_id -> {status, percent, message, ts}
_PROCS: Dict[int, subprocess.Popen] = {}        # movie_id -> ffmpeg 进程（便于取消）


# ------------------------------------------------------------------ ffmpeg 定位
def _bundled_candidates():
    """无配置、不在 PATH 时的兜底候选：组件页安装位 > 便携包自带 tools/ffmpeg。"""
    try:
        from .ext_components import ffmpeg_ext_paths
        for p in ffmpeg_ext_paths():
            yield p
    except Exception:
        pass
    base = Path(__file__).resolve().parent.parent
    yield base / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    yield base / "tools" / "ffmpeg" / "bin" / "ffmpeg"


def _resolve_ffmpeg(cfg: Dict[str, Any]) -> tuple:
    """返回 (ffmpeg, ffprobe)：支持配置绝对路径 / 组件目录 / PATH / 自带 tools/ffmpeg。"""
    ffmpeg = str(cfg.get("ffmpeg_path") or "").strip() or "ffmpeg"
    if not os.path.dirname(ffmpeg) and not shutil.which(ffmpeg):
        for cand in _bundled_candidates():
            if cand.exists():
                ffmpeg = str(cand)
                break
    if os.path.dirname(ffmpeg):
        base_name = os.path.basename(ffmpeg)
        ext = os.path.splitext(base_name)[1] or ".exe"
        ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + ext)
    else:
        ffprobe = "ffprobe"
    return ffmpeg, ffprobe


def probe(path: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """用 ffprobe 探测容器名与首路视频/音频编码。失败返回 {}。"""
    _, ffprobe = _resolve_ffmpeg(cfg)
    try:
        raw = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
        ).stdout.decode("utf-8", "replace")
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    fmt = str((data.get("format") or {}).get("format_name") or "")
    formats = {x.strip() for x in fmt.split(",") if x.strip()}
    vcodec = acodec = ""
    for s in data.get("streams") or []:
        kind = s.get("codec_type")
        # 封面图会被当作 video 流（attached_pic），必须排除
        if kind == "video" and not vcodec and int((s.get("disposition") or {}).get("attached_pic") or 0) != 1:
            vcodec = str(s.get("codec_name") or "").lower()
        elif kind == "audio" and not acodec:
            acodec = str(s.get("codec_name") or "").lower()
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    ext = os.path.splitext(str(path))[1].lower().lstrip(".")
    return {"formats": formats, "ext": ext, "vcodec": vcodec,
            "acodec": acodec, "duration": duration}


def _is_friendly(info: Dict[str, Any]) -> bool:
    """容器与编码是否浏览器可直接播放（按扩展名 + 编码判断）。"""
    ext = str(info.get("ext") or "").lower()
    acodec = info.get("acodec") or ""
    return bool(ext in FRIENDLY_EXTS
                and info.get("vcodec") in FRIENDLY_VCODECS
                and (not acodec or acodec in FRIENDLY_ACODECS))


def analyze(src: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """判断该文件能否被浏览器直接播放 / 是否值得重封装。

    返回 {supported, remuxable, reason, vcodec, acodec, formats, duration}
    - supported=True ：容器与编码都友好，直接播；
    - remuxable=True ：视频是 H.264，只是容器/音轨不兼容 → 重封装后可播；
    - 两个都 False  ：视频编码本身不支持（HEVC 等），走系统播放器。
    """
    info = probe(src, cfg)
    if not info or not info.get("vcodec"):
        return {"supported": None, "remuxable": False, "reason": "probe_failed"}
    base = {k: info[k] for k in ("vcodec", "acodec", "duration", "ext")}
    base["formats"] = sorted(info["formats"])
    if _is_friendly(info):
        return {"supported": True, "remuxable": False, "reason": "", **base}
    # 只有 H.264 能「直接复制」进 MP4；其它编码 remux 后浏览器照样解不了
    if info["vcodec"] == "h264":
        detail = ".%s / %s" % (info.get("ext") or "?", info.get("acodec") or "无音轨")
        return {"supported": False, "remuxable": True,
                "reason": "容器或音轨不兼容（%s）" % detail, **base}
    return {"supported": False, "remuxable": False,
            "reason": "视频编码 %s 浏览器不支持（需转码，建议系统播放器）" % info["vcodec"],
            **base}


# ------------------------------------------------------------------ 产物与状态
def remuxed_path(movie_id: int) -> Path:
    return REMUX_DIR / ("%d.mp4" % int(movie_id))


def is_ready(movie_id: int, src: Optional[str] = None) -> bool:
    """重封装产物是否可用（存在且不早于源文件修改时间）。"""
    out = remuxed_path(movie_id)
    try:
        if not out.exists() or out.stat().st_size <= 0:
            return False
        if src and os.path.exists(src) and out.stat().st_mtime < os.path.getmtime(src):
            return False  # 源文件更新过，缓存失效
        return True
    except OSError:
        return False


def status(movie_id: int) -> Dict[str, Any]:
    with _LOCK:
        st = _STATE.get(int(movie_id))
        return dict(st) if st else {"status": "idle", "percent": 0, "message": ""}


def start(movie_id: int, src: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """启动（或复用）重封装任务，立即返回当前状态；进度用 status() 查询。"""
    mid = int(movie_id)
    with _LOCK:
        cur = _STATE.get(mid)
        if cur and cur.get("status") == "running":
            return dict(cur)
        _STATE[mid] = {"status": "running", "percent": 0, "message": "正在重封装…", "ts": time.time()}
    threading.Thread(target=_run, args=(mid, src, cfg), daemon=True,
                     name="remux-%d" % mid).start()
    return status(mid)


def _run(mid: int, src: str, cfg: Dict[str, Any]) -> None:
    ffmpeg, _ = _resolve_ffmpeg(cfg)
    info = probe(src, cfg)
    duration = float(info.get("duration") or 0)
    try:
        REMUX_DIR.mkdir(parents=True, exist_ok=True)
        out = remuxed_path(mid)
        tmp = out.with_name(out.stem + ".part" + out.suffix)
        # 视频直接复制、音轨转 AAC；+faststart 让 moov 前置，便于边下边播
        cmd = [ffmpeg, "-y", "-i", src,
               "-map", "0:v:0", "-map", "0:a:0?",
               "-c:v", "copy", "-c:a", "aac", "-ac", "2", "-b:a", "192k",
               "-movflags", "+faststart",
               "-progress", "pipe:1", "-nostats",
               "-f", "mp4", str(tmp)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        with _LOCK:
            _PROCS[mid] = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.decode("utf-8", "replace").strip()
            if duration > 0 and text.startswith("out_time_ms="):
                try:
                    sec = int(text.split("=", 1)[1]) / 1_000_000.0
                except ValueError:
                    continue
                pct = max(0, min(99, int(sec / duration * 100)))
                with _LOCK:
                    st = _STATE.setdefault(mid, {})
                    st["percent"] = max(int(st.get("percent") or 0), pct)
        code = proc.wait()
        if code == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(out)
            with _LOCK:
                _STATE[mid] = {"status": "done", "percent": 100,
                               "message": "已重封装，可直接网页播放", "ts": time.time()}
        else:
            tmp.unlink(missing_ok=True)
            with _LOCK:
                _STATE[mid] = {"status": "failed", "percent": 0,
                               "message": "重封装失败（该片可能需要转码，请用系统播放器）",
                               "ts": time.time()}
    except Exception as exc:  # 兜底：绝不把异常抛到线程外
        try:
            remuxed_path(mid).with_name(remuxed_path(mid).stem + ".part" + remuxed_path(mid).suffix).unlink(missing_ok=True)
        except OSError:
            pass
        with _LOCK:
            _STATE[mid] = {"status": "failed", "percent": 0,
                           "message": str(exc)[:200], "ts": time.time()}
    finally:
        with _LOCK:
            _PROCS.pop(mid, None)
        enforce_cache_limit(cfg)


def enforce_cache_limit(cfg: Dict[str, Any]) -> None:
    """按修改时间清理最旧的重封装产物，控制缓存占用。"""
    try:
        limit_mb = int((cfg.get("stream") or {}).get("remux_cache_mb", 4096) or 0)
    except (TypeError, ValueError):
        limit_mb = 4096
    if limit_mb <= 0 or not REMUX_DIR.exists():
        return
    try:
        files = sorted((p for p in REMUX_DIR.glob("*.mp4")),
                       key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    total = 0
    for p in files:
        try:
            total += p.stat().st_size
        except OSError:
            continue
    limit = limit_mb * 1024 * 1024
    for p in files:
        if total <= limit:
            break
        try:
            size = p.stat().st_size
            p.unlink()
            total -= size
        except OSError:
            continue


def clear_cache() -> int:
    """清空重封装缓存，返回删除的文件数。"""
    removed = 0
    if not REMUX_DIR.exists():
        return 0
    for p in REMUX_DIR.glob("*.mp4*"):
        try:
            p.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def cache_stats() -> Dict[str, Any]:
    """缓存占用情况（供设置页展示 / 清理）。"""
    files = list(REMUX_DIR.glob("*.mp4")) if REMUX_DIR.exists() else []
    total = 0
    for p in files:
        try:
            total += p.stat().st_size
        except OSError:
            continue
    return {"count": len(files), "bytes": total}
