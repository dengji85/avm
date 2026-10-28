# -*- coding: utf-8 -*-
"""MetaTube 刮削引擎集成。

MetaTube（https://github.com/metatube-community，Apache-2.0）是一个 Go 写的
刮削服务，覆盖 30+ 个数据源，正好补上本项目没有的 FC2 / 无码厂 / 素人站 /
官方源（fanza、mgstage、sod）。这里只做两件事：

1. HTTP 客户端：把它的 ``/v1/movies/search``、``/v1/movies/:provider/:id``
   翻译成本项目的数据源结果；
2. 可选的进程托管：在 Windows/Linux/macOS 上下载官方二进制并拉起子进程，
   让用户不必自己装 Docker。

设计原则与 ``ext_components`` 保持一致：下载任务放后台线程 + 轮询状态字典，
不引入新依赖，不阻塞主流程；任何一步失败都只影响 MetaTube 这一个数据源。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .config import DATA_DIR, load_config, metatube_cfg

#: 锁定版本：源站解析规则常变，跟随 latest 容易被打断
VERSION = "v1.4.0"
_RELEASE = "https://github.com/metatube-community/metatube-server-releases/releases/download/%s"
#: 国内直连 GitHub 常常失败，按顺序尝试镜像前缀
_MIRRORS = (
    # 实测国内直连 GitHub 约 80KB/s 且中途容易断（16MB 要数分钟），
    # 镜像通常 300KB/s 一分钟内完成，所以镜像优先、官方兜底。
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

# 下载/安装任务状态（前端 1.5s 轮询，与 ext_components 同构）
_TASK: Dict[str, Any] = {"running": False, "phase": "", "percent": 0, "message": "", "log": []}
_LOCK = threading.Lock()
_PROC: Optional[subprocess.Popen] = None
_PROC_PORT = 0


# ---------------------------------------------------------------- 路径与状态


def ext_root() -> str:
    """组件根目录，与扩展组件保持一致（data/ext）。"""
    root = os.path.join(str(DATA_DIR), "ext")
    os.makedirs(root, exist_ok=True)
    return root


def component_dir() -> str:
    d = os.path.join(ext_root(), "metatube")
    os.makedirs(d, exist_ok=True)
    return d


def _exe_name() -> str:
    return "metatube-server.exe" if os.name == "nt" else "metatube-server"


def binary_path() -> str:
    return os.path.join(component_dir(), _exe_name())


def db_path() -> str:
    return os.path.join(component_dir(), "metatube.db")


def installed() -> bool:
    return os.path.isfile(binary_path())


def _meta_path() -> str:
    return os.path.join(component_dir(), "version.json")


def installed_version() -> str:
    try:
        with open(_meta_path(), "r", encoding="utf-8") as f:
            return str((json.load(f) or {}).get("version") or "")
    except Exception:
        return ""


def task_state() -> Dict[str, Any]:
    """安装任务快照（供路由层轮询；与 ext_components.task_state 同名同义）。"""
    with _LOCK:
        return dict(_TASK, log=list(_TASK.get("log") or []))


_task_state = task_state  # 兼容内部旧调用


def _set(**fields: Any) -> None:
    with _LOCK:
        _TASK.update(fields)


def _log(line: str) -> None:
    with _LOCK:
        log = list(_TASK.get("log") or [])
        log.append(str(line))
        _TASK["log"] = log[-6:]


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
        return {"ok": False, "port": 0, "version": "", "error": "MetaTube 未安装，请先下载"}
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
    task = _task_state()
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
        "task": task,
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


# ---------------------------------------------------------------- 安装（下载二进制）


def _asset_name() -> str:
    key = (platform.system(), platform.machine())
    name = _ASSETS.get(key)
    if not name:
        raise RuntimeError("当前平台不受支持：%s/%s" % key)
    return name


def install() -> Dict[str, Any]:
    """后台下载并解压官方二进制（幂等：已安装则直接返回）。"""
    if installed():
        return {"ok": True, "already": True}
    with _LOCK:
        if _TASK.get("running"):
            return {"ok": False, "error": "已有安装任务在进行"}
        _TASK.update({"running": True, "phase": "准备", "percent": 0, "message": "", "log": []})
    t = threading.Thread(target=_run_install, name="metatube-install", daemon=True)
    t.start()
    return {"ok": True}


def _run_install() -> None:
    tmp = os.path.join(component_dir(), "download.zip")
    try:
        asset = _asset_name()
        base = _RELEASE % VERSION + "/" + asset
        urls = [(m + base) for m in _MIRRORS]
        _set(phase="下载", percent=0, message=asset)
        ok = False
        last = ""
        for url in urls:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)  # 上一源留下的半截文件不能续，必须删掉重下
                _download(url, tmp)
                ok = True
                break
            except Exception as e:
                last = str(e)
                _log("下载失败，换下一个源：%s" % last)
        if not ok:
            raise RuntimeError("所有下载源都失败：%s" % last)
        _set(phase="解压", percent=92)
        _extract(tmp, component_dir())
        os.remove(tmp)
        exe = binary_path()
        if not os.path.isfile(exe):
            raise RuntimeError("压缩包里没有找到 %s" % _exe_name())
        if os.name != "nt":
            os.chmod(exe, 0o755)
        _set(phase="校验", percent=96)
        ver = _probe_version(exe)
        with open(_meta_path(), "w", encoding="utf-8") as f:
            json.dump({"version": ver or VERSION, "target": VERSION, "installed_at": int(time.time())},
                      f, ensure_ascii=False)
        _set(running=False, phase="完成", percent=100, message="已安装 %s" % (ver or VERSION))
    except Exception as e:
        _set(running=False, phase="失败", message=str(e))
        _log(str(e))
    finally:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _probe_version(exe: str) -> str:
    flags = 0x08000000 if os.name == "nt" else 0
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, timeout=15,
                             creationflags=flags)
        text = (out.stdout or b"").decode("utf-8", "ignore") + (out.stderr or b"").decode("utf-8", "ignore")
        for tok in text.replace("\n", " ").split():
            if tok.startswith("v") and tok[1:2].isdigit():
                return tok.strip()
    except Exception:
        pass
    return ""


def _download(url: str, dest: str) -> None:
    """流式下载到 dest。失败即抛异常（调用方负责删除半截文件换下一个源）。"""
    import requests
    from .config import outbound_verify_tls

    # 连接 15s、读 120s：GitHub 直连常在读到一半时卡住，读超时太短会白下载
    with requests.get(url, stream=True, timeout=(15, 120), verify=outbound_verify_tls()) as resp:
        if resp.status_code >= 400:
            raise RuntimeError("HTTP %d" % resp.status_code)
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(256 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if total:
                    _set(percent=min(90, int(done * 90 / total)))
    size = os.path.getsize(dest)
    if size < 1024 * 1024:
        raise RuntimeError("下载内容异常（文件过小）")
    if total and size != total:
        raise RuntimeError("下载不完整（%d/%d 字节）" % (size, total))
    # zip 完整性：末段被截断时 Central Directory 会读不出来
    try:
        with zipfile.ZipFile(dest) as z:
            bad = z.testzip()
        if bad:
            raise RuntimeError("压缩包校验失败：%s" % bad)
    except zipfile.BadZipFile:
        raise RuntimeError("下载的压缩包已损坏")


def _extract(archive: str, dest: str) -> None:
    """只解压需要的二进制，防 zip-slip。

    压缩包内的文件名带平台后缀（metatube-server-windows-amd64.exe），
    所以按前缀匹配，统一改名成本程序约定的 ``metatube-server[.exe]``。
    """
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            name = os.path.basename(info.filename).lower()
            if not name.startswith("metatube-server"):
                continue
            if not (name.endswith(".exe") or "." not in name):
                continue
            target = os.path.join(dest, _exe_name())
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            return
    raise RuntimeError("压缩包结构异常：未找到可执行文件")


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
