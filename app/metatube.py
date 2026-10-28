# -*- coding: utf-8 -*-
"""MetaTube 刮削引擎集成。

MetaTube（https://github.com/metatube-community，Apache-2.0）是一个 Go 写的
刮削服务，覆盖 30+ 个数据源，正好补上本项目没有的 FC2 / 无码厂 / 素人站 /
官方源（fanza、mgstage、sod）。

职责划分：
* 二进制的下载 / 解压 / 登记 / 自检 → ``ext_components``（cid=metatube），
  与 ffmpeg、ASR 组件同一套框架（断点续传、进度轮询、卸载、导入）；
* 服务客户端与子进程托管 → 本模块（search / movie / start / stop / 自启）。

注意：源站解析规则常变，安装走官方 Release 并锁定版本。
"""
from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .config import load_config, metatube_cfg

#: 锁定版本：源站解析规则常变，跟随 latest 容易被打断
VERSION = "v1.4.0"
_RELEASE = "https://github.com/metatube-community/metatube-server-releases/releases/download/%s"
#: 实测国内直连 GitHub 约 80KB/s 且中途容易断（16MB 要数分钟），
#: 镜像通常 300KB/s 一分钟内完成，所以镜像优先、官方兜底。
_MIRRORS = (
    "https://ghfast.top/",
    "https://gh-proxy.com/",
    "",
)

_ASSETS = {
    ("Windows", "AMD64"): "metatube-server-windows-amd64.zip",
    ("Windows", "ARM64"): "metatube-server-windows-arm64.zip",
    ("Linux", "x86_64"): "metatube-server-linux-amd64.zip",
    ("Linux", "aarch64"): "metatube-server-linux-arm64.zip",
    ("Linux", "arm64"): "metatube-server-linux-arm64.zip",
    ("Darwin", "x86_64"): "metatube-server-darwin-amd64.zip",
    ("Darwin", "arm64"): "metatube-server-darwin-arm64.zip",
}

_PROC: Optional[subprocess.Popen] = None
_PROC_PORT = 0


# ---------------------------------------------------------------- 路径与安装状态


def asset_name() -> str:
    key = (platform.system(), platform.machine())
    name = _ASSETS.get(key)
    if not name:
        raise RuntimeError("当前平台不受支持：%s/%s" % key)
    return name


def asset_urls() -> List[str]:
    """按「镜像优先、官方兜底」给出完整下载地址列表。"""
    base = _RELEASE % VERSION + "/" + asset_name()
    return [m + base for m in _MIRRORS]


def component_dir() -> str:
    """组件目录：跟 ext_components 的 ext_root 约定走（可被 env / 配置改位置）。"""
    from .ext_components import component_dir as _cid_dir
    return _cid_dir("metatube")


def _exe_name() -> str:
    return "metatube-server.exe" if os.name == "nt" else "metatube-server"


def binary_path() -> str:
    return os.path.join(component_dir(), _exe_name())


def db_path() -> str:
    return os.path.join(component_dir(), "metatube.db")


def installed() -> bool:
    return os.path.isfile(binary_path())


def installed_version() -> str:
    """已安装版本（登记在 ext_components 的 manifest 里）。"""
    try:
        from .ext_components import _load_manifest
        info = (_load_manifest().get("components") or {}).get("metatube") or {}
        return str(info.get("version") or "")
    except Exception:
        return ""


def probe_version(exe: str) -> str:
    """运行 ``--version`` 取版本号（安装后自检用）。"""
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, timeout=15,
                             creationflags=flags)
        text = (out.stdout or b"").decode("utf-8", "ignore") + \
            (out.stderr or b"").decode("utf-8", "ignore")
        for tok in text.replace("\n", " ").split():
            if tok.startswith("v") and tok[1:2].isdigit():
                return tok.strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- HTTP 客户端


def _base_url(cfg: Dict[str, Any]) -> str:
    if _PROC is not None and _PROC.poll() is None and _PROC_PORT:
        return "http://127.0.0.1:%d" % _PROC_PORT
    return str(cfg.get("base_url") or "http://127.0.0.1:18080").rstrip("/")


def _headers(token: str) -> Dict[str, str]:
    h = {"Accept": "application/json"}
    if token:
        # 服务端 auth.Token 同时接受 Bearer 与 X-TOKEN，两个都带上更稳
        h["Authorization"] = "Bearer %s" % token
        h["X-TOKEN"] = str(token)
    return h


def _request(cfg: Dict[str, Any], path: str, params: Optional[Dict[str, Any]] = None,
             timeout: Optional[float] = None) -> Any:
    """GET 请求，返回 ``data`` 字段；失败抛 RuntimeError（消息可直接展示给用户）。"""
    import requests
    from .config import outbound_verify_tls

    scfg = cfg.get("_scraper") or load_config().get("scraper") or {}
    proxy = str(scfg.get("proxy") or "").strip()
    url = _base_url(cfg) + path
    # 本机地址绝不能走 scraper.proxy：用户常把代理设成 127.0.0.1:10808，
    # 代理连不上本地端口就回 503，看起来像「MetaTube 挂了」，实际是绕了一圈。
    host = (urlparse(url).hostname or "").lower()
    local = host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")
    proxies = {"http": proxy, "https": proxy} if (proxy and not local) else None
    try:
        resp = requests.get(
            url, params=params, headers=_headers(str(cfg.get("token") or "")),
            timeout=timeout or float(cfg.get("timeout") or 15),
            proxies=proxies,
            verify=outbound_verify_tls(),
        )
    except Exception as e:  # 连接拒绝 / DNS / 超时 / TLS
        raise RuntimeError("连接 MetaTube 失败：%s" % e)
    if resp.status_code == 401:
        raise RuntimeError("MetaTube 拒绝了令牌（401），请检查 token")
    if resp.status_code == 404:
        raise RuntimeError("MetaTube 没有命中（404）")
    if resp.status_code >= 400:
        body = (resp.text or "").strip().replace("\n", " ")[:160]
        hint = ""
        if resp.status_code in (502, 503, 504):
            hint = "（若填的是本机地址，请确认服务已启动且未被代理拦截）"
        raise RuntimeError("MetaTube 返回 HTTP %d%s %s" % (resp.status_code, hint, body))
    try:
        payload = resp.json()
    except Exception:
        raise RuntimeError("MetaTube 返回的不是 JSON")
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    return payload.get("data") if isinstance(payload, dict) else payload


def health(base_url: str = "", token: str = "") -> Dict[str, Any]:
    """连接测试：GET / 返回 {ok, version, error}。"""
    cfg = metatube_cfg()
    if base_url:
        cfg["base_url"] = base_url
    if token:
        cfg["token"] = token
    # 托管模式且进程没起来时，先给可操作的提示，而不是让用户看到连接被拒/503
    if (not base_url and str(cfg.get("mode")) == "managed" and installed() and not running()):
        return {"ok": False, "app": "", "version": "",
                "error": "托管服务未启动：请点击「启动服务」"}
    try:
        data = _request(cfg, "/", timeout=6)
        return {"ok": True, "app": (data or {}).get("app", ""),
                "version": (data or {}).get("version", ""), "error": ""}
    except Exception as e:
        return {"ok": False, "app": "", "version": "", "error": str(e)}


def providers() -> Dict[str, Any]:
    return _request(metatube_cfg(), "/v1/providers") or {}


def search(code: str) -> List[Dict[str, Any]]:
    data = _request(metatube_cfg(), "/v1/movies/search", {"q": code, "fallback": "true"})
    return data if isinstance(data, list) else []


def movie(provider: str, movie_id: str) -> Dict[str, Any]:
    return _request(metatube_cfg(), "/v1/movies/%s/%s" % (provider, movie_id)) or {}


# ---------------------------------------------------------------- 字段映射


def _num(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _rating(score: Any) -> str:
    """MetaTube 的 score 多为 0-5 或 0-10，本项目评分是 0-5，统一夹取。"""
    try:
        v = float(score)
    except Exception:
        return ""
    if v <= 0:
        return ""
    if v > 5:
        v = v / 2 if v <= 10 else 5.0
    return ("%.1f" % min(5.0, v)).rstrip("0").rstrip(".")


def to_meta(info: Dict[str, Any]) -> Dict[str, Any]:
    """MovieInfo -> 本项目元数据字段（见 providers.base.FIELDS）。"""
    label = str(info.get("label") or "").strip()
    maker = str(info.get("maker") or "").strip()
    rating = _rating(info.get("score"))
    meta: Dict[str, Any] = {
        "title": str(info.get("title") or "").strip(),
        "code": str(info.get("number") or "").strip(),
        "plot": str(info.get("summary") or "").strip(),
        "release_date": str(info.get("release_date") or "").strip(),
        "runtime": info.get("runtime") or "",
        # label 更接近「厂牌」，maker 退到发行商；只有 maker 时就当厂牌
        "studio": label or maker,
        "publisher": maker if label and maker and maker != label else "",
        "series": str(info.get("series") or "").strip(),
        "director": str(info.get("director") or "").strip(),
        "cover": str(info.get("cover_url") or "").strip(),
        "fanart": str(info.get("big_cover_url") or info.get("big_thumb_url") or "").strip(),
        "actresses": [str(a).strip() for a in (info.get("actors") or []) if str(a).strip()],
        "genres": [str(g).strip() for g in (info.get("genres") or []) if str(g).strip()],
    }
    # 没有评分就不要给键：normalize 会把空 rating 折算成 0.0，进而把已有评分清零
    if rating:
        meta["rating"] = rating
    return meta


def pick_exact(hits: List[Dict[str, Any]], code: str) -> Optional[Dict[str, Any]]:
    """fallback=true 会带回近似结果，必须按番号严格比对，避免张冠李戴。"""
    want = _num(code)
    if not want:
        return hits[0] if hits else None
    exact = [h for h in hits if _num(str(h.get("number") or "")) == want]
    if exact:
        # 官方源优先（fanza/mgstage/sod 的字段质量高于第三方站）
        official = ("fanza", "mgstage", "sod", "faleno", "duga")
        exact.sort(key=lambda h: 0 if str(h.get("provider") or "") in official else 1)
        return exact[0]
    return None


# ---------------------------------------------------------------- 进程托管


def _free_port(start: int) -> int:
    for port in range(int(start), int(start) + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return int(start)


def running() -> bool:
    return _PROC is not None and _PROC.poll() is None


def _wait_ready(port: int, token: str, timeout: float = 25.0) -> Dict[str, Any]:
    cfg = metatube_cfg()
    cfg.update({"base_url": "http://127.0.0.1:%d" % port, "token": token})
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if not running():
            return {"ok": False, "error": "进程已退出：" + last}
        try:
            data = _request(cfg, "/", timeout=3)
            return {"ok": True, "version": str((data or {}).get("version") or ""), "error": ""}
        except Exception as e:
            last = str(e)
            time.sleep(0.6)
    return {"ok": False, "error": "等待就绪超时：%s" % last}


def start() -> Dict[str, Any]:
    """拉起托管进程（幂等）。返回 {ok, port, version, error}。"""
    global _PROC, _PROC_PORT
    cfg = metatube_cfg()
    if running():
        return {"ok": True, "port": _PROC_PORT, "version": installed_version(), "error": ""}
    if not installed():
        return {"ok": False, "port": 0, "version": "", "error": "MetaTube 未安装，请先在扩展组件里下载"}
    token = str(cfg.get("token") or "").strip()
    if not token:
        # 托管模式首次启动生成随机令牌并落盘：服务虽然只绑 127.0.0.1，
        # 但任意本地页面仍能发请求，加个令牌成本极低。
        import secrets
        token = secrets.token_urlsafe(24)
        try:
            from .config import update_config
            update_config({"metatube": {"token": token}})
            cfg["token"] = token
        except Exception:
            pass
    port = _free_port(int(cfg.get("port") or 18080))
    exe = binary_path()
    log_path = os.path.join(component_dir(), "metatube.log")
    cmd = [exe, "--bind", "127.0.0.1", "--port", str(port), "--dsn", db_path()]
    if token:
        cmd += ["--token", token]
    env = dict(os.environ)
    # 源站在墙外时，复用主程序的代理设置（Go 的 http 栈自动识别这两个变量）
    scfg = load_config().get("scraper") or {}
    proxy = str(scfg.get("proxy") or "").strip()
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW：不弹黑框
    try:
        log = open(log_path, "ab")
        _PROC = subprocess.Popen(cmd, cwd=component_dir(), stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=flags, env=env)
        _PROC_PORT = port
    except Exception as e:
        return {"ok": False, "port": port, "version": "", "error": "启动失败：%s" % e}
    res = _wait_ready(port, token)
    if res.get("ok"):
        return {"ok": True, "port": port, "version": res.get("version") or installed_version(),
                "error": ""}
    stop()
    return {"ok": False, "port": port, "version": "", "error": res.get("error") or "启动失败"}


def stop() -> Dict[str, Any]:
    global _PROC, _PROC_PORT
    proc = _PROC
    _PROC = None
    _PROC_PORT = 0
    if proc is None or proc.poll() is not None:
        return {"ok": True}
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=8)
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=6)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    return {"ok": True}


def status() -> Dict[str, Any]:
    cfg = metatube_cfg()
    st: Dict[str, Any] = {
        "enabled": bool(cfg.get("enabled")),
        "mode": str(cfg.get("mode") or "external"),
        "base_url": _base_url(cfg),
        "installed": installed(),
        "installed_version": installed_version(),
        "token_set": bool(str(cfg.get("token") or "").strip()),
        "running": running(),
        "pid": _PROC.pid if running() else 0,
        "port": _PROC_PORT,
        "target_version": VERSION,
        "error": "",
    }
    if bool(cfg.get("enabled")):
        # 只做一次轻量探活，失败不抛异常（设置页要能显示「连不上」）
        try:
            data = _request(cfg, "/", timeout=3)
            st["server_version"] = str((data or {}).get("version") or "")
            st["reachable"] = True
        except Exception as e:
            st["server_version"] = ""
            st["reachable"] = False
            st["error"] = str(e)
    return st


# ---------------------------------------------------------------- 启动钩子


def autostart() -> None:
    """程序启动时调用：仅 managed + auto_start 时拉起，失败静默（不拖慢主服务）。"""
    cfg = metatube_cfg()
    if not bool(cfg.get("enabled")) or str(cfg.get("mode")) != "managed":
        return
    if not bool(cfg.get("auto_start")) or not installed():
        return
    try:
        start()
    except Exception:
        pass


def shutdown() -> None:
    try:
        stop()
    except Exception:
        pass
