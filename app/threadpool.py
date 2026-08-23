# -*- coding: utf-8 -*-
"""全局共享线程池。

统一承载「耗时后台任务」（扫描、刮削、女优资料抓取等），便于：
  - 限制并发，避免多任务同时占满系统资源；
  - 统一管理生命周期（daemon 线程，随进程退出）；
  - 统一命名前缀，便于日志/排查。

用法：
    from .threadpool import submit_task
    future = submit_task(fn, *args, **kwargs)
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

#: 全局线程池（守护线程）。max_workers 取 CPU 核数与 4 的较小值，最大 4，
#: 既保证并行吞吐，又避免与单次任务内部子线程池（如刮削自身的 pool）叠加过多。
_MAX_WORKERS = 4
_SHARED_POOL = ThreadPoolExecutor(
    max_workers=_MAX_WORKERS,
    thread_name_prefix="avm-task",
)


def submit_task(fn, *args, **kwargs):
    """向全局线程池提交一个后台任务，返回 Future。"""
    return _SHARED_POOL.submit(fn, *args, **kwargs)


def shared_pool() -> ThreadPoolExecutor:
    """暴露线程池（如需在任务内部再开子池时使用）。"""
    return _SHARED_POOL
