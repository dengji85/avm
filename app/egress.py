"""智能出站：按主机判断「能不能直连」，能直连就不绕代理。

为什么：在线来源里相当一部分站点 / CDN 本身就能直连，只有被墙或被 Cloudflare
拦的才需要走代理。统一套代理的代价是：多一跳、多一次 CONNECT + TLS，跨境代理
还常常只有几 Mbps —— 明明直连能跑满带宽的站点，被硬塞进了慢出口。

做法（结论先行，能解释）：
* 判定粒度是**主机**（同一个站点的页面 / CDN 分片通常共用一套可达性）；
* 三种状态：``direct``（直连可用）/ ``proxy``（必须走代理）/ ``空``（还没测过）；
* 没测过时**仍然默认走代理**（保守，保证能播），同时后台用 Range 0–1023 的
  HEAD 式小请求试一次直连，结果写库并记住 6 小时；
* 判定只归档两类可靠结论：请求成功 → 直连；连接超时 / 拒绝 / 403 → 走代理。
  404 / 410 这种多半是签名过期，**不下结论**，避免把「地址过期」误判成「直连不可用」。

不做 / 不猜的：
* 不做地区判定、不读 IP 归属，纯以「这次请求通不通」为依据；
* 不主动在中途切换正在播放的主机（签名可能与出口 IP 绑定，中途换出口会 403），
  只在验证阶段或首次出现时探测，判定后 6 小时内保持不变；
* 内网 / 本机地址仍由 ``proxy._direct_host`` 强制直连，与代理配置无关。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from .db import db, query_one
from .proxy import _direct_host, global_proxy

# 判定有效期：6 小时后重新试直连（节点漂移 / 站点放开地区限制都能跟上）
_TTL = 6 * 3600
# 内存缓存：避免每个分片都查一次库
_MEM: Dict[str, tuple] = {}
_MEM_TTL = 30
# 正在探测的主机，避免重复发请求
_INFLIGHT: set = set()
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _host(url: str) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def smart_enabled() -> bool:
    from .config import load_config
    return bool((load_config().get("stream") or {}).get("smart_egress", True))


def _read(host: str) -> Dict[str, Any]:
    with db() as conn:
        return query_one(conn, "SELECT mode, latency_ms, checked_ts FROM host_egress "
                               "WHERE host = ?", (host,)) or {}


def _write(host: str, mode: str, latency_ms: int, note: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO host_egress(host, mode, latency_ms, note, checked_ts) "
            "VALUES(?,?,?,?,?) ON CONFLICT(host) DO UPDATE SET "
            "mode=excluded.mode, latency_ms=excluded.latency_ms, "
            "note=excluded.note, checked_ts=excluded.checked_ts",
            (host, mode, int(latency_ms), note, int(time.time())))
        conn.commit()
    _MEM[host] = (mode, time.time())


def mode(url: str) -> str:
    """``direct`` / ``proxy`` / ``""``（没测过或已过期）。"""
    host = _host(url)
    if not host:
        return ""
    now = time.time()
    hit = _MEM.get(host)
    if hit and now - hit[1] < _MEM_TTL:
        return hit[0]
    row = _read(host)
    m = str(row.get("mode") or "")
    ts = int(row.get("checked_ts") or 0)
    if m and ts and now - ts > _TTL:
        m = ""                      # 过期：当作没测过，下次请求重新探
    _MEM[host] = (m, now)
    return m


def proxies_for(url: str = "", override: str = "") -> Optional[Dict[str, str]]:
    """流媒体出站：能直连返回 ``None``，否则返回代理字典。

    未知主机：先按代理走（保证能播），同时后台探一次直连，下次请求就能用上。
    """
    proxy = str(override or "").strip() or global_proxy()
    if not proxy or _direct_host(url):
        return None
    if not smart_enabled():
        return {"http": proxy, "https": proxy}
    m = mode(url)
    if m == "direct":
        return None
    if not m:
        probe_async(url)
    return {"http": proxy, "https": proxy}


def probe_async(url: str, headers: Optional[Dict[str, str]] = None) -> None:
    """后台试一次直连（同一主机 6 小时内只试一次）。"""
    if not smart_enabled():
        return
    host = _host(url)
    if not host or host in _INFLIGHT or mode(url):
        return
    _INFLIGHT.add(host)
    try:
        from .threadpool import submit_task
        submit_task(_probe, host, url, dict(headers or {}))
    except Exception:
        _INFLIGHT.discard(host)


def _probe(host: str, url: str, headers: Dict[str, str]) -> None:
    """直连小请求：成功 = direct；连不上 / 403 = proxy；其他状态码不下结论。"""
    import requests
    mode_out, latency, note = "", 0, ""
    try:
        hdrs = {"User-Agent": _UA, "Accept": "*/*", "Range": "bytes=0-1023"}
        for k, v in (headers or {}).items():
            if str(k).lower() in ("referer", "origin", "user-agent"):
                hdrs[k] = v
        t0 = time.time()
        # proxies 显式置 None：只关掉环境变量里的 HTTP(S)_PROXY，才是真正的「直连」
        r = requests.get(url, headers=hdrs, timeout=(3, 5), stream=True,
                         allow_redirects=True,
                         proxies={"http": None, "https": None})
        latency = int((time.time() - t0) * 1000)
        code = r.status_code
        try:
            r.close()
        except Exception:
            pass
        if code < 400:
            mode_out = "direct"
            note = "直连可用（%d ms）" % latency
        elif code in (403, 451):
            mode_out = "proxy"
            note = "直连被拒 HTTP %d（疑似地区限制）" % code
        # 404 / 410 等：多半是签名过期，留空 = 不下结论
    except Exception as exc:
        mode_out = "proxy"
        note = "直连失败：%s" % type(exc).__name__
    finally:
        _INFLIGHT.discard(host)
        if mode_out:
            try:
                _write(host, mode_out, latency, note)
            except Exception:
                pass
