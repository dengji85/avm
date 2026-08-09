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
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    websocket = None


# ------------------------------------------------------------------ 工具


def _is_blocker(html: str) -> bool:
    """判断拿到的页面是否仍是 av-wiki 的反爬验证页。"""
    if not html:
        return False
    low = html.lower()
    if ("loader" in low and "正在验证" in html) or "请稍候" in html or "正在验证您的请求" in html:
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

    def fetch(self, url: str, wait: int = 20, poll: float = 1.5) -> Optional[str]:
        """导航到 url 并返回最终 HTML。会等待反爬验证页自行放行。

        始终新建一个专用 tab 来抓取（不干扰用户正在浏览的页面），
        复用 Chrome 的会话（cookie / localStorage / 信任度）以绕过 av-wiki 验证。
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
                    last_html = html
                    if not _is_blocker(html):
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


# ------------------------------------------------------------------ 常驻 Chrome 管理器


class ChromeManager:
    """自管理一个常驻 headless Chrome（带调试端口），供 CDP 抓取复用。

    进程级单例：整个 Python 进程内只维护一个 Chrome，多次 ``ensure()`` 幂等。
    失败时 ``ensure()`` 返回 False，调用方应回退到普通 requests。
    """

    def __init__(self, port: int = 9222, user_data_dir: Optional[str] = None,
                 chrome_path: Optional[str] = None):
        self.port = port
        self.user_data_dir = user_data_dir or str(
            Path(__file__).resolve().parent.parent / "data" / "chrome_profile"
        )
        self.chrome_path = chrome_path or _find_chrome()
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
                    "--headless=new",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "--disable-extensions",
                    "--disable-sync",
                    "--remote-allow-origins=*",
                ]
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


def get_manager(port: int = 9222, user_data_dir: Optional[str] = None) -> ChromeManager:
    with _MANAGERS_LOCK:
        if port not in _MANAGERS:
            _MANAGERS[port] = ChromeManager(port=port, user_data_dir=user_data_dir)
        return _MANAGERS[port]


def cdp_fetch(url: str, port: int = 9222, wait: int = 20,
              user_data_dir: Optional[str] = None,
              auto_launch: bool = True) -> Optional[str]:
    """便捷函数：连接本机 Chrome 调试端口抓取 url，失败返回 None。

    ``auto_launch=True`` 时若端口未监听，会自动拉起一个常驻 headless Chrome
    （使用 ``user_data_dir`` 指定的独立 profile），从而支持完全后台自动采集。
    """
    if auto_launch:
        mgr = get_manager(port, user_data_dir)
        if not mgr.ensure(wait + 10):
            return None
    sess = CDPSession(port=port, timeout=wait + 10)
    try:
        if not sess.connect():
            return None
        return sess.fetch(url, wait=wait)
    except Exception:
        return None
    finally:
        sess.close()
