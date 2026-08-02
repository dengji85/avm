# -*- coding: utf-8 -*-
"""
外部播放器观看监控（A 档 · 通用方案，零配置）。

背景
----
「系统播放」是把文件交给系统默认播放器（VLC / PotPlayer / MPC / mpv …）独立进程播放，
浏览器和播放器之间没有通道，拿不到进度。本模块在后端（与播放器同机）启动一个后台守护
线程，周期性探测「目标文件是否仍被播放器占用」，从而在不依赖任何播放器内部接口的情况下，
还原出「看了多久 + 哪几段时间段」，落库到 watch_sessions。

探测原理
--------
* Windows：用 ctypes 以「独占」(FILE_SHARE_NONE) 方式尝试打开文件，若返回共享冲突
  (ERROR_SHARING_VIOLATION=32 / ERROR_LOCK_VIOLATION=33) 即说明有进程（播放器）正持有
  该文件句柄 —— 视为「在播」。
* 其他平台：监控启动播放器时拿到的子进程 PID 是否存活（仅能给出整体时长，分段退化为单段）。

把「文件被占用 / 进程存活」的时间片段合并为观看区间（segments）；播放器关闭、或超过安全
上限后结束监控并落库。
"""
from __future__ import annotations

import os
import time
import threading
from typing import List, Optional, Tuple

from . import store
from .db import db

# ------------------------------------------------------------------ 参数
POLL = 5                 # 探测间隔（秒）
GRACE = 90               # 文件释放后继续等待的宽限（秒），避开瞬时关闭误判
DEFAULT_CAP = 6 * 3600   # 拿不到影片时长时的最长监控（秒）
MIN_WATCHED = 5          # 低于该秒数且无可合并区间，视为「未真正观看」，丢弃该场次

_lock = threading.Lock()
_active: dict = {}       # session_id -> threading.Thread（仅保活引用，避免被回收）


# ------------------------------------------------------------------ 占用探测
def _file_busy_windows(path: str) -> bool:
    """以独占方式试探打开，若被其他进程占用则返回 True。"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        GENERIC_READ = 0x80000000
        FILE_SHARE_NONE = 0
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x80
        INVALID = 0xFFFFFFFF
        h = kernel32.CreateFileW(
            ctypes.c_wchar_p(path), GENERIC_READ, FILE_SHARE_NONE,
            None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        if h == INVALID or h == -1:
            err = ctypes.GetLastError()
            # 32: ERROR_SHARING_VIOLATION  33: ERROR_LOCK_VIOLATION
            return err in (32, 33)
        kernel32.CloseHandle(h)
        return False
    except Exception:
        return False


def _file_busy(path: str) -> bool:
    if os.name == "nt":
        return _file_busy_windows(path)
    # 非 Windows：尝试以建议锁 / 独占方式打开，被占用则抛异常
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_EXLOCK"):  # *BSD / macOS
            flags |= os.O_EXLOCK
        fd = os.open(path, flags)
        os.close(fd)
        return False
    except (OSError, AttributeError):
        return True


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def _merge(segments: List[List[float]], gap: float) -> List[List[float]]:
    if not segments:
        return []
    segments = sorted(segments, key=lambda s: s[0])
    out: List[List[float]] = [list(segments[0])]
    for s, e in segments[1:]:
        if s - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


# ------------------------------------------------------------------ 监控线程
def _monitor(movie_id: int, path: str, session_id: int, cap: float,
             pid: Optional[int]) -> None:
    target = os.path.abspath(path)
    intervals: List[List[float]] = []
    cur_start: Optional[float] = None
    last_open = time.time()
    started = time.time()
    try:
        while True:
            now = time.time()
            if os.name == "nt":
                open_now = _file_busy(target)
            else:
                open_now = _pid_alive(pid)
            if open_now:
                last_open = now
                if cur_start is None:
                    cur_start = now
            else:
                if cur_start is not None:
                    intervals.append([cur_start, now])
                    cur_start = None
            if not open_now and (now - last_open) >= GRACE:
                break
            if (now - started) >= cap:
                break
            time.sleep(POLL)
    finally:
        if cur_start is not None:
            intervals.append([cur_start, time.time()])

    segments = _merge(intervals, gap=POLL)
    watched = sum(max(0.0, e - s) for s, e in segments)

    try:
        with db() as conn:
            if watched < MIN_WATCHED and not segments:
                # 用户其实没看（例如点错 / 播放器未真正打开文件），丢弃该空场次
                conn.execute("DELETE FROM watch_sessions WHERE id=?", (session_id,))
            else:
                store.end_session(conn, session_id, end_pos=0.0,
                                  watched_sec=round(watched, 1),
                                  finished=0, segments=segments)
                conn.execute("UPDATE movies SET watched=1 WHERE id=?", (movie_id,))
    except Exception:
        pass


def start_external_monitor(movie_id: int, path: str, session_id: int,
                           runtime_sec: float = 0.0,
                           pid: Optional[int] = None) -> None:
    cap = (runtime_sec + DEFAULT_CAP) if runtime_sec and runtime_sec > 0 else (24 * 3600)
    t = threading.Thread(
        target=_monitor, args=(movie_id, path, session_id, cap, pid), daemon=True)
    with _lock:
        _active[session_id] = t
    t.start()
