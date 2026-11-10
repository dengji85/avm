"""线程局部 requests.Session 池：让上游请求复用 TCP/TLS 连接。

为什么需要：走代理抓在线来源时，每个分片都重新 ``requests.get`` 意味着每段都要
重新「连代理 → CONNECT 隧道 → 与 CDN TLS 握手」，跨境代理下这笔握手常常
0.5–1.5 秒，比下载分片本身还久，表现就是「每段起播前先卡一下」。

这里按线程缓存一个 Session（``requests.Session`` 非线程安全，必须一线程一份），
连接池给足 8 条，播放列表、分片、预取共用同一套连接，后续请求直接复用。

取舍：
* 只做连接复用，不改并发语义；并发上限仍由调用方（预取任务数）控制；
* ``Accept-Encoding: identity``：分片要按原字节透传 / 缓存，别让 gzip 掺一脚；
* 不主动重试（``max_retries=0``）：HLS 的重试语义在播放器与续期逻辑里，
  后端偷偷重试容易把 403 变成长时间卡住；
* 代理或 CDN 主动断开空闲连接时，urllib3 会在下一次请求自动新建，无需处理。
"""

from __future__ import annotations

import threading

import requests

_LOCAL = threading.local()


def session() -> requests.Session:
    """当前线程的 Session（首次调用时创建）。"""
    sess = getattr(_LOCAL, "sess", None)
    if sess is None:
        sess = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8,
                                                max_retries=0)
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)
        sess.headers.update({"Accept-Encoding": "identity"})
        _LOCAL.sess = sess
    return sess
