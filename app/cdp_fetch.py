# -*- coding: utf-8 -*-
"""通过 Chrome DevTools Protocol (CDP) 抓取网页。

av-wiki.net 使用 LiteSpeed 的「请稍候…」验证页：新会话会被卡在每 5 秒自动
reload 的循环里，只有积累了信任度的真实浏览器会话才会被放行。因此常规的
requests/cookie 复制方案无效。

本模块**自管理一个常驻 Chrome**：
- 启动时用固定的 ``--user-data-dir``（默认 ``data/chrome_profile``）拉起一个
  headless Chrome 并打开调试端口；
- 该 profile 一旦在磁盘上养出对 av-wiki 的信任度（首次手动访问一次即可持久化），
  之后完全后台运行，不依赖用户日常使用的 Chrome，也不弹出任何窗口；
- 抓取任务在需要时连这个常驻 Chrome，抓完不关它（可复用）；
- 若常驻 Chrome 没起来（例如没装 Chrome），``cdp_fetch`` 返回 None，调用方回退
  到普通 requests 方案。

这样 avwiki 数据源可以像 javbus / javdb 一样被调度层**全自动**调度。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    websocket = None


# ------------------------------------------------------------------ 工具


def _title_of(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip().lower() if m else ""


def is_error_page(html: str) -> bool:
    """Chrome 自己的错误页（``ERR_CONNECTION_TIMED_OUT`` 等）。

    这类页面会把请求 URL 回显在正文里，若当成站点内容，会把番号误判成「有资源」。
    """
    if not html:
        return False
    low = html.lower()
    return ("chrome-error://" in low or "err_connection" in low
            or 'id="reload-button"' in low)


def _is_blocker(html: str) -> bool:
    """判断拿到的页面是否仍是 av-wiki 的反爬验证页（或空白壳页）。

    av-wiki 对未建立信任度的会话会返回：
      - 旧的「请稍候…」reload 循环页（含 loader / 正在验证 字样）；
      - 新版几乎纯 JS 渲染的空白壳页（body 无实质文本，只有占位脚本）。
    后者 ``body`` 文本极短，必须识别为未放行，否则会把空白当成功、
    写入空标题/空演员。这里用「含验证字样」或「正文实质内容过短」双重判定。

    注意：正常页面里也会出现「请稍候…」（例如登录按钮的 loading 文案），
    所以只认 **标题** 是验证页的那种，避免把正常作品页判成未放行。
    """
    if not html:
        return True
    low = html.lower()
    title = _title_of(html)
    if ("loader" in low and "正在验证" in html) or "正在验证您的请求" in html:
        return True
    if title.startswith(("请稍候", "稍候", "just a moment", "attention required")):
        return True
    # 去掉脚本/样式后看正文长度：正常作品页至少有几百字，
    # 空白壳页通常 < 50 字（只有占位符/菜单名）。
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "form", "nav", "footer"]):
            t.decompose()
        text = soup.get_text(" ", strip=True)
    except Exception:
        text = html
    if len(text) < 80:
        return True
    return False


def _http_get_json(url: str, timeout: int = 8):
    req = urllib.request.Request(url, headers={"Origin": "http://127.0.0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _find_chrome() -> Optional[str]:
    """按常见路径查找 Chrome 可执行文件。"""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Users\{}\AppData\Local\Google\Chrome\Application\chrome.exe".format(os.environ.get("USERNAME", "")),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for c in candidates:
        try:
            if c and Path(c).exists():
                return c
        except Exception:
            continue
    # 回退：依赖 PATH
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        loc = shutil.which(name)
        if loc:
            return loc
    return None


# ------------------------------------------------------------------ CDP 客户端


class CDPSession:
    """一个到本机 Chrome 调试端口的轻量 CDP 客户端。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 9222, timeout: int = 30):
        self.base = f"http://{host}:{port}"
        self.timeout = timeout
        self.ws = None
        self._id = 0

    def connect(self) -> bool:
        if websocket is None:
            return False
        try:
            ver = _http_get_json(f"{self.base}/json/version", timeout=5)
            ws_url = ver["webSocketDebuggerUrl"]
            self.ws = websocket.create_connection(ws_url, timeout=self.timeout)
            return True
        except Exception:
            self.ws = None
            return False

    def _send(self, method: str, params=None, session_id: Optional[str] = None) -> int:
        self._id += 1
        payload = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        self.ws.send(json.dumps(payload))
        return self._id

    def _recv(self, want_id: int):
        while True:
            raw = self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == want_id:
                return msg

    def fetch(self, url: str, wait: int = 20, poll: float = 1.5,
              require: Optional[Any] = None) -> Optional[str]:
        """导航到 url 并返回最终 HTML。会等待反爬验证页自行放行。

        始终新建一个专用 tab 来抓取（不干扰用户正在浏览的页面），
        复用 Chrome 的会话（cookie / localStorage / 信任度）以绕过 av-wiki 验证。

        ``require``：可选判定函数，页面已不是验证页但内容还没「落定」时（单页应用
        先出空壳再填数据），继续轮询直到它返回真或超时，避免把半成品当最终页面。
        """
        if not self.ws:
            return None
        # 新建 tab 并 attach
        cid = self._send("Target.createTarget", {"url": "about:blank"})
        res = self._recv(cid)
        tid = res.get("result", {}).get("targetId")
        if not tid:
            return None
        aid = self._send("Target.attachToTarget", {"targetId": tid, "flatten": True})
        res = self._recv(aid)
        session_id = res.get("result", {}).get("sessionId")
        if not session_id:
            return None

        try:
            self._send("Page.enable", session_id=session_id)
            # 等导航回执：Chrome 的 Page.navigate 回执虽然在慢站点上要等较久，
            # 但它代表导航已落定；不等就轮询会拿到 about:blank 或旧文档。
            nid = self._send("Page.navigate", {"url": url}, session_id)
            self._recv(nid)

            # 轮询：反爬页会每 5s reload，等待其放行；最多等 wait 秒
            deadline = time.time() + wait
            last_html = None
            while time.time() < deadline:
                time.sleep(poll)
                gid = self._send(
                    "Runtime.evaluate",
                    {"expression": "document.documentElement.outerHTML", "returnByValue": True},
                    session_id,
                )
                res = self._recv(gid)
                html = res.get("result", {}).get("result", {}).get("value")
                if html:
                    if is_error_page(html):
                        # 网络层失败（连不上 / 超时）：错误页不可当内容，直接判失败
                        return None
                    last_html = html
                    if not _is_blocker(html):
                        if require is None:
                            return html
                        try:
                            if require(html):
                                return html
                        except Exception:
                            return html
            return last_html
        finally:
            # 关闭专用 tab，避免堆积
            try:
                self._send("Target.closeTarget", {"targetId": tid})
            except Exception:
                pass

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发一条 CDP 命令并等结果（供取 Cookie / UA 这类一次性查询用）。"""
        if not self.ws:
            return {}
        cid = self._send(method, params or {})
        return self._recv(cid).get("result") or {}


# ------------------------------------------------------------------ 常驻 Chrome 管理器


class ChromeManager:
    """自管理一个常驻 headless Chrome（带调试端口），供 CDP 抓取复用。

    进程级单例：整个 Python 进程内只维护一个 Chrome，多次 ``ensure()`` 幂等。
    失败时 ``ensure()`` 返回 False，调用方应回退到普通 requests。
    """

    def __init__(self, port: int = 9222, user_data_dir: Optional[str] = None,
                 chrome_path: Optional[str] = None, headless: bool = True,
                 proxy: str = ""):
        self.port = port
        self.user_data_dir = user_data_dir or str(
            Path(__file__).resolve().parent.parent / "data" / "chrome_profile"
        )
        self.chrome_path = chrome_path or _find_chrome()
        self.headless = headless
        self.proxy = (proxy or "").strip()
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self.started = False

    # 调试端口是否已可连接
    def _port_up(self) -> bool:
        try:
            _http_get_json(f"http://127.0.0.1:{self.port}/json/version", timeout=3)
            return True
        except Exception:
            return False

    def ensure(self, timeout: int = 20) -> bool:
        """确保常驻 Chrome 已就绪（已启动则直接复用，否则拉起）。返回是否可用。"""
        with self.lock:
            if self._port_up():
                self.started = True
                return True
            if self.proc is not None and self.proc.poll() is None:
                # 进程在但端口没起来，等一会儿
                return self._wait_port(timeout)
            if not self.chrome_path or not Path(self.chrome_path).exists():
                return False
            try:
                Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
                args = [
                    self.chrome_path,
                    f"--remote-debugging-port={self.port}",
                    f"--user-data-dir={self.user_data_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "--disable-extensions",
                    "--disable-sync",
                    "--remote-allow-origins=*",
                ]
                if self.headless:
                    args.append("--headless=new")
                else:
                    # 非 headless：Cloudflare 这类站点会识别 headless 并一直卡挑战页，
                    # 真实浏览器几秒就放行。窗口挪到屏幕外，不打扰用户。
                    args.extend(["--window-position=-32000,-32000",
                                 "--window-size=1000,800"])
                if self.proxy:
                    args.append(f"--proxy-server={self.proxy}")
                # 容器 / 受限环境可追加参数，如 --no-sandbox --disable-dev-shm-usage
                _extra = os.environ.get("AVM_CHROME_ARGS", "").split()
                if _extra:
                    args.extend(_extra)
                self.proc = subprocess.Popen(
                    args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                )
            except Exception:
                return False
            self.started = self._wait_port(timeout)
            return self.started

    def _wait_port(self, timeout: int) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._port_up():
                return True
            time.sleep(0.5)
        return False

    def stop(self) -> None:
        with self.lock:
            if self.proc is not None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=5)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                self.proc = None
            self.started = False


# 进程级单例管理器
_MANAGERS: Dict[int, ChromeManager] = {}
_MANAGERS_LOCK = threading.Lock()


def has_desktop() -> bool:
    """当前环境有没有可用的桌面会话（决定能否用非 headless 的「真」浏览器）。"""
    if os.name == "nt" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def get_manager(port: int = 9222, user_data_dir: Optional[str] = None,
                headless: bool = True, proxy: str = "") -> ChromeManager:
    """按端口取进程级单例管理器（同端口复用已拉起的 Chrome）。"""
    with _MANAGERS_LOCK:
        if port not in _MANAGERS:
            _MANAGERS[port] = ChromeManager(
                port=port, user_data_dir=user_data_dir,
                headless=headless, proxy=proxy,
            )
        return _MANAGERS[port]


def cdp_fetch(url: str, port: int = 9222, wait: int = 20,
              user_data_dir: Optional[str] = None,
              auto_launch: bool = True, headless: bool = True,
              proxy: str = "", require: Optional[Any] = None) -> Optional[str]:
    """便捷函数：连接本机 Chrome 调试端口抓取 url，失败返回 None。

    ``auto_launch=True`` 时若端口未监听，会自动拉起一个常驻 Chrome
    （使用 ``user_data_dir`` 指定的独立 profile），从而支持完全后台自动采集。
    ``headless=False`` 会拉起窗口在屏幕外的真实浏览器——Cloudflare 这类站点
    只有在真实浏览器里才会通过挑战；``proxy`` 用于给浏览器本身设出口代理。
    """
    if auto_launch:
        mgr = get_manager(port, user_data_dir, headless=headless, proxy=proxy)
        if not mgr.ensure(wait + 10):
            return None
    sess = CDPSession(port=port, timeout=wait + 10)
    try:
        if not sess.connect():
            return None
        return sess.fetch(url, wait=wait, require=require)
    except Exception:
        return None
    finally:
        sess.close()


def cdp_cookies(url: str, port: int = 9222,
                auto_launch: bool = True) -> Tuple[str, str]:
    """取 Chrome 里对 ``url`` 生效的 Cookie 与 UA，返回 ``(cookie串, user_agent)``。

    用途：CDP 通道过了反爬验证后，把 ``cf_clearance`` 这类凭证连同当时的 UA 存下来，
    后续直接走普通请求即可，不必每次重跑浏览器验证。取不到返回 ``("", "")``。
    """
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lstrip(".").lower()
    if not host:
        return "", ""
    if auto_launch:
        mgr = get_manager(port)
        if not mgr.ensure(25):
            return "", ""
    sess = CDPSession(port=port, timeout=20)
    try:
        if not sess.connect():
            return "", ""
        cookies = sess.call("Storage.getCookies").get("cookies") or []
        now = time.time()
        parts = []
        for c in cookies:
            domain = str(c.get("domain") or "").lstrip(".").lower()
            if not domain or not (host == domain or host.endswith("." + domain)):
                continue
            expires = c.get("expires")
            if isinstance(expires, (int, float)) and 0 < expires < now:
                continue
            name = str(c.get("name") or "")
            if not name:
                continue
            parts.append(f"{name}={c.get('value') or ''}")
        ua = str(sess.call("Browser.getVersion").get("userAgent") or "")
        return "; ".join(parts), ua
    except Exception:
        return "", ""
    finally:
        sess.close()
