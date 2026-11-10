# -*- coding: utf-8 -*-
"""无头浏览器取回在线来源的播放地址（服务端续期的兜底通道）。

为什么需要这个模块
    jable.tv 一类源站对服务端 HTTP 请求返回 Cloudflare 人机校验（403 +
    ``Just a moment...``），``cloudscraper`` 也一样；123av 这类站点页面里
    没有内联 m3u8，播放地址要等页面脚本拉起播放器后才会请求。静态抓取
    两条路都走不通，只有在**本机**跑一个真实浏览器内核才能拿到新签名地址。

与「不执行页面脚本」约定的取舍
    * 只在后台巡检 / 用户点「重新获取」时打开**来源页面本身**，不注入站点
      Cookie、不登录、不绕过付费或 DRM；截获到的地址随后仍按来源 SSRF 规则
      逐条校验，通过后才写库；
    * 浏览器是一次性会话：任务结束即关闭，不常驻内存、不做指纹伪造工业化；
    * 依赖 Playwright + 本机 Chrome / Edge：缺失时返回可读原因，由调用方降级
      成「服务端 HTTP 抓取 + 提示用浏览器插件回写」，不影响主服务启动。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from .config import load_config

#: 单飞锁：同时只开一个浏览器（内存 / CPU 边界，也避免被站点判定为爬虫集群）
_LOCK = threading.Lock()

#: 人机校验页的标题特征（Cloudflare / 常见 WAF）
_CHALLENGE = ("just a moment", "attention required", "checking your browser",
              "verify you are human", "请稍候", "正在检查")

#: 浏览器内核候选：优先复用本机已装浏览器，最后才用 Playwright 自带 Chromium
_CHANNELS: List[Optional[str]] = ["chrome", "msedge", None]

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _stream_cfg() -> Dict[str, Any]:
    return dict(load_config().get("stream") or {})


def available() -> bool:
    """无头浏览器依赖是否可用（未安装 Playwright 时返回 False，不抛异常）。"""
    try:
        import playwright  # noqa: F401
    except Exception:
        return False
    return True


def _proxy() -> Optional[Dict[str, str]]:
    """出站代理：与播放 / 验证共用 ``stream.upstream_proxy`` > ``network.proxy``。"""
    from .proxy import global_proxy
    url = str(_stream_cfg().get("upstream_proxy") or "").strip() or global_proxy()
    return {"server": url} if url else None


def _origin(u: str) -> str:
    """取站点 origin（``https://a.b``，无尾斜杠）；非法或非 http(s) 返回空串。"""
    try:
        p = urlsplit(str(u or ""))
        if p.scheme in ("http", "https") and p.netloc:
            return "%s://%s" % (p.scheme, p.netloc)
    except ValueError:
        pass
    return ""


def _frame_url(req: Any) -> str:
    """发起该请求的 frame 地址（播放器 iframe 本身就是防盗链认的跳板域）。"""
    try:
        return str(getattr(req.frame, "url", "") or "")
    except Exception:
        return ""


def _capture(req: Any, state: Dict[str, Any], page_origin: str) -> None:
    """网络层截获播放器真正请求的 .m3u8，连同它实际生效的 Referer / Origin。

    只记这两项：CDN 的防盗链校验常见的是「播放器所在跳板域的 Referer」，
    不是影片页地址；把它带回下游，验证与播放才能用同一套头。Referer 头拿
    不到时退回发起请求的 frame 地址（播放器 iframe 就在那个跳板域上）。
    """
    try:
        url = str(getattr(req, "url", "") or "")
        found: List[str] = state["found"]
        if ".m3u8" not in url.lower() or len(found) >= 6:
            return
        if url not in found:
            found.append(url)
        headers: Dict[str, Dict[str, str]] = state["headers"]
        if url in headers:
            return
        try:
            raw = req.all_headers() or {}
        except Exception:
            raw = {}
        ref = str(raw.get("referer") or "").strip()
        org = str(raw.get("origin") or "").strip()
        frame = _frame_url(req)
        if not ref.startswith(("http://", "https://")):
            ref = frame if frame.startswith(("http://", "https://")) else ""
        if not ref:
            return
        if not org.startswith(("http://", "https://")):
            org = _origin(ref)
        pick = {"Referer": ref}
        if org:
            pick["Origin"] = org
        headers[url] = pick
        # 跨站的播放器跳板域记成候选 Referer：capture 拿不全时还能试出来
        for cand in (_origin(ref), _origin(frame)):
            if cand and cand != page_origin and cand not in state["referers"]:
                state["referers"].append(cand)
    except Exception:
        pass


def _frame_origins(page: Any, page_origin: str) -> List[str]:
    """页面上所有跨站 iframe 的 origin（播放器跳板常以 iframe 形式出现）。"""
    out: List[str] = []
    try:
        frames = list(page.frames)
    except Exception:
        return out
    for frame in frames:
        try:
            cand = _origin(frame.url)
        except Exception:
            continue
        if cand and cand != page_origin and cand not in out and len(out) < 3:
            out.append(cand)
    return out


def _challenge_passed(page: Any) -> bool:
    """标题不再是 Cloudflare 校验页即视为通过（拿不到标题按未通过处理）。"""
    try:
        title = str(page.title() or "").strip().lower()
    except Exception:
        return False
    if not title:
        return False
    return not any(marker in title for marker in _CHALLENGE)


def _fail(note: str, started: float) -> Dict[str, Any]:
    return {"ok": False, "urls": [], "headers": {}, "referers": [], "note": note,
            "seconds": round(time.time() - started, 1), "challenge": False}


def extract(page_url: str, timeout: float = 0.0) -> Dict[str, Any]:
    """打开来源页面，等待人机校验通过并截获 m3u8 请求。

    返回 ``{"ok", "urls", "headers", "referers", "note", "seconds", "challenge"}``；
    任何异常都转成 ``note`` 返回，绝不向调用方抛——续期流程不能因为浏览器不可用
    而中断整批。``headers`` 是「候选地址 → 实际 Referer / Origin」，
    ``referers`` 是页面上跨站 iframe / 播放器跳板域的 origin（兜底候选）。
    """
    started = time.time()
    try:
        from .stream_verify import _url_problem
    except Exception:                      # pragma: no cover 循环导入兜底
        _url_problem = None
    if _url_problem is not None:
        problem, reason = _url_problem(page_url)
        if problem:
            return _fail("页面地址不可访问：%s" % reason, started)
    try:
        limit = float(timeout or _stream_cfg().get("browser_timeout") or 30)
    except (TypeError, ValueError):
        limit = 30.0
    limit = max(8.0, min(120.0, limit))
    if not _LOCK.acquire(blocking=False):
        return _fail("已有另一个浏览器任务在跑，本次跳过", started)
    try:
        return _extract_locked(page_url, limit, started)
    finally:
        _LOCK.release()


def _extract_locked(page_url: str, limit: float, started: float) -> Dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return _fail("未安装无头浏览器组件（pip install playwright），已跳过", started)

    insecure = bool((load_config().get("server") or {}).get("insecure_skip_verify", False))
    args = ["--disable-blink-features=AutomationControlled", "--no-first-run",
            "--disable-extensions", "--mute-audio", "--disable-background-networking"]
    if insecure:
        args.append("--ignore-certificate-errors")

    found: List[str] = []
    headers: Dict[str, Dict[str, str]] = {}
    state: Dict[str, Any] = {"found": found, "headers": headers, "referers": []}
    page_origin = _origin(page_url)
    passed = False
    err = ""
    with sync_playwright() as pw:
        browser = None
        for channel in _CHANNELS:
            try:
                if channel:
                    browser = pw.chromium.launch(channel=channel, headless=True,
                                                 args=args, proxy=_proxy())
                else:
                    browser = pw.chromium.launch(headless=True, args=args, proxy=_proxy())
                break
            except Exception as exc:       # 换下一个内核，全部失败再报原因
                err = "%s 不可用（%s）" % (channel or "chromium", type(exc).__name__)
                browser = None
        if browser is None:
            return _fail("没有可用的浏览器内核：%s" % err, started)
        try:
            ctx = browser.new_context(
                user_agent=_UA, viewport={"width": 1366, "height": 768},
                locale="zh-CN", ignore_https_errors=insecure)
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = ctx.new_page()
            page.on("request", lambda req: _capture(req, state, page_origin))
            try:
                page.goto(page_url, wait_until="domcontentloaded",
                          timeout=int(min(20.0, limit) * 1000))
            except Exception:
                pass                       # 挑战页 / 慢加载都交给下面的轮询判断
            deadline = started + limit
            while time.time() < deadline:
                passed = passed or _challenge_passed(page)
                if passed and found:
                    break
                try:
                    page.wait_for_timeout(1000)
                except Exception:
                    break
            passed = passed or _challenge_passed(page)
            # 关闭前补一轮跨站 iframe：Referer 头拿不全时用它们的 origin 兜底
            for cand in _frame_origins(page, page_origin):
                if cand not in state["referers"]:
                    state["referers"].append(cand)
            try:
                ctx.close()
            except Exception:
                pass
        finally:
            try:
                browser.close()
            except Exception:
                pass

    secs = round(time.time() - started, 1)
    referers: List[str] = list(state["referers"])[:3]
    if found:
        return {"ok": True, "urls": found, "headers": headers, "referers": referers,
                "note": "已用无头浏览器取回播放地址（%.1fs）" % secs,
                "seconds": secs, "challenge": passed}
    if not passed:
        return {"ok": False, "urls": [], "headers": {}, "referers": referers,
                "note": "源站人机校验未通过（%.1fs），已跳过" % secs,
                "seconds": secs, "challenge": False}
    return {"ok": False, "urls": [], "headers": {}, "referers": referers,
            "note": "页面已打开但未请求到 m3u8（%.1fs）" % secs,
            "seconds": secs, "challenge": True}
