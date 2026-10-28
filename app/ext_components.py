# -*- coding: utf-8 -*-
"""可选扩展组件：ffmpeg / 语音识别依赖 / 识别模型。

主程序保持单文件小体积（PyInstaller 明确排除 ASR 依赖，ffmpeg 也不打包），
这些组件按需装到 data/ext/，同样支持离线手动导入。本模块统一负责：

* 检测：组件是否就绪、版本、磁盘占用、装在哪
* 安装：流式下载（可取消、断点续传）→ 安全解压 → 校验 → 登记 manifest
* 导入：用户自己下好的 zip / 已解压目录，校验后登记
* 卸载：删目录、清 manifest（不动用户显式配置的路径）

设计约束：
* 同一时间只允许一个安装任务（下载 + 解压是磁盘重活）
* 安装只写 data/ext，不碰 ffmpeg_path / asr.libs_dir 等用户配置；
  解析优先级永远是「用户显式配置 > 组件目录 > 系统 PATH」
* 容器（AVM_CONTAINER=1）里 ffmpeg 由镜像预装，检测后如实标注来源
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .config import DATA_DIR, IN_CONTAINER, load_config
from .threadpool import submit_task

log_prefix = "[ext]"

# ------------------------------------------------------------------ 下载源
# ffmpeg 官方静态构建（按平台给候选，按顺序尝试，前面失败自动换下一个）
FFMPEG_SOURCES: Dict[str, List[str]] = {
    "nt": [
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip",
    ],
    "posix": [
        "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
    ],
}

# 语音识别依赖的预打包 zip（pip install --target 产物，发布页提供）。
# 没发布 / 链接失效时走手动导入，UI 会给出明确提示。
ASR_LIBS_URLS = [
    "https://github.com/dengji85/avm/releases/latest/download/asr-libs-cp311-win64.zip",
]

# 模型走 ModelScope 镜像（国内直连 HuggingFace 常超时；与 tools/fetch_asr_model.py 同源）
MODEL_REPOS = {
    "small": "pengzhendong/faster-whisper-small",
}
MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")

PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

_LOCK = threading.Lock()
_TASK: Dict[str, Any] = {"running": False}


class _Cancelled(Exception):
    pass


# ------------------------------------------------------------------ 目录与登记
def ext_root(refresh: bool = False) -> str:
    """组件根目录：环境变量 > ext.dir 配置 > data/ext。"""
    env = os.environ.get("AVM_EXT_DIR", "").strip()
    if env:
        return env
    cfg = load_config(refresh=refresh)
    d = str((cfg.get("ext") or {}).get("dir") or "").strip()
    if d:
        return d
    return str(DATA_DIR / "ext")


def component_dir(cid: str) -> str:
    if cid == "ffmpeg":
        return os.path.join(ext_root(), "ffmpeg", "bin")
    if cid == "asr-libs":
        return os.path.join(ext_root(), "asr-libs")
    if cid == "metatube":
        return os.path.join(ext_root(), "metatube")
    if cid.startswith("asr-model-"):
        size = cid.rsplit("-", 1)[-1]
        return os.path.join(str(DATA_DIR), "models", "faster-whisper-%s" % size)
    raise ValueError("未知组件：%s" % cid)


def _manifest_path() -> str:
    return os.path.join(ext_root(), "manifest.json")


def _load_manifest() -> Dict[str, Any]:
    try:
        with open(_manifest_path(), encoding="utf-8") as f:
            data = json.loads(f.read() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_manifest(m: Dict[str, Any]) -> None:
    try:
        os.makedirs(ext_root(), exist_ok=True)
        tmp = _manifest_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(m, ensure_ascii=False, indent=2))
        os.replace(tmp, _manifest_path())
    except Exception:
        pass


def _record(cid: str, **fields: Any) -> None:
    m = _load_manifest()
    info = m.setdefault("components", {}).setdefault(cid, {})
    info.update(fields)
    info["installed_at"] = int(time.time())
    _save_manifest(m)


def _forget(cid: str) -> None:
    m = _load_manifest()
    (m.get("components") or {}).pop(cid, None)
    _save_manifest(m)


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ------------------------------------------------------------------ 检测
def ffmpeg_ext_paths() -> List[Path]:
    """组件页安装的 ffmpeg 候选路径（remux 等模块复用）。"""
    root = Path(ext_root()) / "ffmpeg" / "bin"
    names = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg",)
    return [root / n for n in names]


def ffmpeg_exe(cfg: Optional[Dict[str, Any]] = None) -> str:
    """功能侧统一 ffmpeg 路径解析：用户配置 > 组件目录 > PATH > 便携包自带。

    不做版本探测（无子进程），抽帧 / 预览 / 转封装等热路径可安全调用。
    组件页在线安装只写 data/ext、不碰 ffmpeg_path 配置——所有功能必须走这里，
    否则会出现「面板显示已就绪、功能却报 ffmpeg 不可用」的分裂。
    找不到时返回用户配置原值或 "ffmpeg"，交由调用方按旧逻辑报错。"""
    c = cfg if cfg is not None else load_config()
    cand = str(c.get("ffmpeg_path") or "").strip()
    if cand and os.path.isfile(cand):
        return cand
    for p in ffmpeg_ext_paths():
        if p.exists():
            return str(p)
    w = shutil.which("ffmpeg")
    if w:
        return w
    base = Path(__file__).resolve().parent.parent
    names = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg",)
    for n in names:
        cc = base / "tools" / "ffmpeg" / "bin" / n
        if cc.exists():
            return str(cc)
    return cand or "ffmpeg"


def ffprobe_exe(cfg: Optional[Dict[str, Any]] = None) -> str:
    """与 ffmpeg 同目录的 ffprobe；配置只有裸名时回退 PATH。"""
    ff = ffmpeg_exe(cfg)
    d = os.path.dirname(ff)
    if not d:
        return shutil.which("ffprobe") or "ffprobe"
    name = "ffprobe.exe" if ff.lower().endswith(".exe") else "ffprobe"
    p = os.path.join(d, name)
    return p if os.path.isfile(p) else ff


def _ffmpeg_version(exe: str) -> str:
    try:
        r = subprocess.run([exe, "-version"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=15)
        first = (r.stdout or b"").decode("utf-8", "replace").splitlines()
        if first:
            m = re.search(r"version\s+(\S+)", first[0])
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


def _detect_ffmpeg() -> Dict[str, Any]:
    """解析优先级：用户配置 > 组件目录 > PATH > 便携包自带 tools/ffmpeg。"""
    cfg = load_config(refresh=True)
    exe, src = "", ""
    cand = str(cfg.get("ffmpeg_path") or "").strip()
    custom = cand                        # 用户显式配置值（即使文件不存在也要回显给前端）
    if cand and os.path.isfile(cand):
        exe, src = cand, "config"
    if not exe:
        for p in ffmpeg_ext_paths():
            if p.exists():
                exe, src = str(p), "ext"
                break
    if not exe:
        w = shutil.which("ffmpeg")
        if w:
            exe, src = w, "path"
    if not exe:
        base = Path(__file__).resolve().parent.parent
        names = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg",)
        for n in names:
            c = base / "tools" / "ffmpeg" / "bin" / n
            if c.exists():
                exe, src = str(c), "bundled"
                break
    ver = _ffmpeg_version(exe) if exe else ""
    m = (_load_manifest().get("components") or {}).get("ffmpeg") or {}
    size = m.get("size") or 0
    if exe and src == "ext" and not size:
        size = _dir_size(os.path.dirname(exe))
    return {
        "id": "ffmpeg", "group": "basic", "state": "ready" if exe else "missing",
        "version": ver, "path": exe, "source": src, "size": size,
        "custom": custom,   # 用户显式配置的路径（可能不存在，前端编辑框回显用）
    }


def asr_libs_dir() -> str:
    """组件页安装的依赖目录（存在才返回，供 asr.libs_dir 兜底）。"""
    d = component_dir("asr-libs")
    return d if os.path.isdir(d) else ""


def _has_pkg(d: str, pkg: str) -> bool:
    files = os.listdir(d) if os.path.isdir(d) else []
    return pkg in files or any(x.startswith(pkg + "-") for x in files)


def _detect_asr_libs() -> Dict[str, Any]:
    from . import asr
    ok = asr.lib_available()
    path = asr.libs_dir()
    src = ""
    if ok:
        ext_d = component_dir("asr-libs")
        if path and os.path.normpath(path) == os.path.normpath(ext_d):
            src = "ext"
        elif os.environ.get("AVM_ASR_LIBS", "").strip():
            src = "env"
        elif str((load_config().get("asr") or {}).get("libs_dir") or "").strip():
            src = "config"
        else:
            src = "system"
    m = (_load_manifest().get("components") or {}).get("asr-libs") or {}
    size = m.get("size") or 0
    if src == "ext" and not size:
        size = _dir_size(ext_d)
    return {
        "id": "asr-libs", "group": "speech", "state": "ready" if ok else "missing",
        "version": m.get("version") or "", "path": path if ok else "",
        "source": src, "size": size,
    }


def _detect_model(size: str) -> Dict[str, Any]:
    from . import asr
    root = asr.model_root(size)
    ok = os.path.isfile(os.path.join(root, "model.bin"))
    return {
        "id": "asr-model-%s" % size, "group": "speech", "state": "ready" if ok else "missing",
        "version": "CTranslate2", "path": root, "source": "dir",
        "size": _dir_size(root) if ok else 0,
    }


def selftest(cid: str) -> Dict[str, Any]:
    """组件自检：真实验证「能不能用」（ffmpeg 跑 -version、依赖真实 import、
    模型文件逐个核对），把「面板显示就绪但功能不能用」这类问题一步定位。"""
    if cid == "ffmpeg":
        return _selftest_ffmpeg()
    if cid == "asr-libs":
        return _selftest_asr_libs()
    if cid == "metatube":
        return _selftest_metatube()
    if cid.startswith("asr-model-"):
        return _selftest_model(cid.rsplit("-", 1)[-1])
    raise ValueError("未知组件: %s" % cid)


def _selftest_ffmpeg() -> Dict[str, Any]:
    cfg = load_config(refresh=True)
    checks: List[Dict[str, Any]] = []
    exe = ffmpeg_exe(cfg)
    ok = True
    if os.path.isfile(exe):
        checks.append({"name": "ffmpeg 路径", "ok": True, "detail": exe})
    else:
        ok = False
        checks.append({"name": "ffmpeg 路径", "ok": False,
                       "detail": "未找到（配置 / 组件目录 / PATH / 便携包都没命中）"})
    if os.path.isfile(exe):
        try:
            r = subprocess.run([exe, "-version"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=15)
            first = (r.stdout or b"").decode("utf-8", "replace").splitlines()[:1]
            if r.returncode == 0:
                checks.append({"name": "ffmpeg 可运行", "ok": True,
                               "detail": (first[0][:120] if first else "返回码 0")})
            else:
                ok = False
                checks.append({"name": "ffmpeg 可运行", "ok": False,
                               "detail": "退出码 %d：%s" % (r.returncode, (first[0][:100] if first else "无输出"))})
        except Exception as e:
            ok = False
            checks.append({"name": "ffmpeg 可运行", "ok": False, "detail": str(e)[:160]})
    probe = ffprobe_exe(cfg)
    pok = probe != exe and os.path.isfile(probe)
    if not pok:
        pok = bool(shutil.which(probe)) if os.path.dirname(probe) else bool(shutil.which("ffprobe"))
    checks.append({"name": "ffprobe（探测时长/封装用）", "ok": pok,
                   "detail": probe if pok else "未找到——预览图 / 智能封面 / 对白提取需要它"})
    if not pok:
        ok = False
    d = _detect_ffmpeg()
    return {"ok": ok, "version": d.get("version") or "", "source": d.get("source") or "",
            "path": exe, "checks": checks}


def _selftest_asr_libs() -> Dict[str, Any]:
    import importlib
    from . import asr
    d = asr.libs_dir()
    checks: List[Dict[str, Any]] = [{
        "name": "依赖目录", "ok": bool(d) and os.path.isdir(d),
        "detail": d or "未配置（空 = 只认 PATH 里的全局安装）",
    }]
    asr._ensure_libs()
    ok = True
    for mod in ("faster_whisper", "ctranslate2"):
        try:
            spec = importlib.util.find_spec(mod)
            if spec and getattr(spec, "origin", None):
                checks.append({"name": "import %s" % mod, "ok": True, "detail": spec.origin})
            else:
                ok = False
                checks.append({"name": "import %s" % mod, "ok": False, "detail": "找不到——依赖没装或路径不对"})
        except Exception as e:
            ok = False
            checks.append({"name": "import %s" % mod, "ok": False, "detail": str(e)[:140]})
    return {"ok": ok, "checks": checks}


def _selftest_metatube() -> Dict[str, Any]:
    """MetaTube 自检：二进制能跑 + 服务可达 + 已启用为数据源。"""
    from . import metatube
    checks: List[Dict[str, Any]] = []
    ok = True
    exe = metatube.binary_path()
    if os.path.isfile(exe):
        checks.append({"name": "二进制", "ok": True, "detail": exe})
    else:
        ok = False
        checks.append({"name": "二进制", "ok": False,
                       "detail": "未安装：点击上方「安装」下载官方包"})
    if os.path.isfile(exe):
        ver = metatube.probe_version(exe)
        if ver:
            checks.append({"name": "可运行", "ok": True, "detail": ver})
        else:
            ok = False
            checks.append({"name": "可运行", "ok": False,
                           "detail": "--version 执行失败（可能被杀软拦截）"})
    try:
        pv = metatube.providers() or {}
        movies = pv.get("movie_providers") or {}
        checks.append({"name": "数据源", "ok": True,
                       "detail": "影片源 %d 个 / 演员源 %d 个" % (
                           len(movies), len(pv.get("actor_providers") or {}))})
    except Exception as e:
        ok = False
        checks.append({"name": "服务", "ok": False, "detail": str(e)[:160]})
    mt_cfg = load_config().get("metatube") or {}
    enabled = bool(mt_cfg.get("enabled"))
    checks.append({"name": "启用为数据源", "ok": True,
                   "detail": "已启用" if enabled else "未启用（设置 → 刮削数据源 里开启）"})
    return {"ok": ok, "checks": checks}


def _selftest_model(size: str) -> Dict[str, Any]:
    from . import asr
    root = asr.model_root(size)
    checks: List[Dict[str, Any]] = [{
        "name": "模型目录", "ok": os.path.isdir(root), "detail": root,
    }]
    ok = os.path.isdir(root)
    for f in ("model.bin", "config.json", "tokenizer.json"):
        p = os.path.join(root, f)
        present = os.path.isfile(p)
        detail = p if present else "缺失"
        if present:
            detail += "（%.1f MB）" % (os.path.getsize(p) / 1048576.0)
        else:
            ok = False
        checks.append({"name": f, "ok": present, "detail": detail})
    return {"ok": ok, "checks": checks}


def _detect_metatube() -> Dict[str, Any]:
    """MetaTube 刮削引擎：安装状态 + 运行状态一起给前端。"""
    from . import metatube
    st = metatube.status()
    m = (_load_manifest().get("components") or {}).get("metatube") or {}
    installed = bool(st.get("installed"))
    if installed and not st.get("reachable"):
        state = "ready"
    elif installed:
        state = "ready"
    else:
        state = "missing"
    return {
        "id": "metatube", "group": "scraper",
        "state": state,
        "installed": installed,
        "version": st.get("installed_version") or "",
        "path": metatube.binary_path() if installed else "",
        "source": "ext" if installed else "",
        "size": m.get("size") or (_dir_size(metatube.component_dir()) if installed else 0),
        # 服务态（扩展组件页的专属操作按钮要用）
        "running": bool(st.get("running")),
        "reachable": bool(st.get("reachable")),
        "enabled": bool(st.get("enabled")),
        "mode": st.get("mode"),
        "token_set": bool(st.get("token_set")),
        "base_url": st.get("base_url"),
        "server_version": st.get("server_version") or "",
        "error": st.get("error") or "",
    }


def status() -> Dict[str, Any]:
    """全部组件状态 + 当前安装任务（前端扩展组件页就靠这一个接口）。"""
    comps = [_detect_ffmpeg(), _detect_asr_libs()]
    from . import asr
    size = str((load_config(refresh=True).get("asr") or {}).get("model") or "small")
    for s in ("small", "medium"):
        c = _detect_model(s)
        c["active"] = (s == size)
        comps.append(c)
    comps.append(_detect_metatube())
    with _LOCK:
        task = dict(_TASK)
    return {
        "components": comps,
        "task": task,
        "ext_dir": ext_root(),
        "container": bool(IN_CONTAINER),
        "pip_available": _pip_cmd() is not None,
    }


# ------------------------------------------------------------------ 任务状态
def task_state() -> Dict[str, Any]:
    with _LOCK:
        return dict(_TASK)


def _set(**fields: Any) -> None:
    with _LOCK:
        _TASK.update(fields)


def _log(msg: str) -> None:
    with _LOCK:
        lines = _TASK.setdefault("log", [])
        lines.append("%s %s" % (time.strftime("%H:%M:%S"), msg))
        del lines[:-6]


def cancel() -> Dict[str, Any]:
    with _LOCK:
        if _TASK.get("running"):
            _TASK["cancel"] = True
            _TASK["message"] = "正在取消…"
    return {"ok": True}


# ------------------------------------------------------------------ 下载 / 解压
def _check_cancel() -> None:
    with _LOCK:
        if _TASK.get("cancel"):
            raise _Cancelled()


def _download(url: str, dest: str) -> str:
    """流式下载（支持 .part 断点续传），把进度写进任务状态。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    headers = {}
    done = 0
    if os.path.isfile(tmp):
        done = os.path.getsize(tmp)
        if done > 0:
            headers["Range"] = "bytes=%d-" % done
    t0 = time.time()
    with requests.get(url, stream=True, timeout=60, headers=headers) as r:
        if r.status_code == 200 and done > 0:
            done = 0          # 服务器不支持 Range：从头下
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0) + done
        _set(total=total, downloaded=done)
        mode = "ab" if done else "wb"
        with open(tmp, mode) as f:
            for chunk in r.iter_content(1024 * 1024):
                _check_cancel()
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - t0 >= 0.5:
                    speed = done / max(0.1, now - t0)
                    _set(downloaded=done, total=total or done,
                         percent=int(done * 100.0 / total) if total else 0,
                         speed=int(speed),
                         message="下载中 %.1f MB/s" % (speed / 1048576.0))
                    t0 = now
    os.replace(tmp, dest)
    return dest


def _safe_members_zip(zf: zipfile.ZipFile, dest: str) -> None:
    base = os.path.realpath(dest)
    for info in zf.infolist():
        target = os.path.realpath(os.path.join(dest, info.filename))
        if not target.startswith(base + os.sep) and target != base:
            raise ValueError("压缩包内路径不安全：%s" % info.filename)


def _extract(archive: str, dest: str) -> None:
    """解压 zip / tar.*，带路径校验（防 zip-slip）。"""
    os.makedirs(dest, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            _safe_members_zip(zf, dest)
            zf.extractall(dest)
        return
    with tarfile.open(archive, "r:*") as tf:
        base = os.path.realpath(dest)
        for m in tf.getmembers():
            target = os.path.realpath(os.path.join(dest, m.name))
            if not target.startswith(base + os.sep) and target != base:
                raise ValueError("压缩包内路径不安全：%s" % m.name)
        tf.extractall(dest)


def _find_file(root: str, name: str) -> str:
    low = name.lower()
    for dirpath, _dirs, files in os.walk(root):
        if name in files:
            return os.path.join(dirpath, name)
        if low in {f.lower() for f in files}:
            return os.path.join(dirpath, name)
    return ""


def _fetch_archive(urls: List[str], dest: str, what: str) -> str:
    """按顺序尝试多个下载源，全部失败抛 RuntimeError。返回落盘路径。"""
    last = ""
    for i, url in enumerate(urls):
        try:
            _log("下载 " + url)
            _download(url, dest if i == 0 else "%s.%d" % (dest, i))
            return dest if i == 0 else "%s.%d" % (dest, i)
        except _Cancelled:
            raise
        except Exception as e:                           # noqa: BLE001 换下一个源
            last = str(e)
            _log("源失败：%s" % last[:140])
    raise RuntimeError("%s 所有下载源都失败了：%s" % (what, last[:200]))


# ------------------------------------------------------------------ 安装：ffmpeg
def _install_ffmpeg(source: str) -> Dict[str, Any]:
    _set(state="downloading", percent=0, message="准备下载…", downloaded=0, total=0)
    arch = os.path.join(ext_root(), "_tmp")
    os.makedirs(arch, exist_ok=True)
    pkg = _fetch_archive(FFMPEG_SOURCES.get("nt" if os.name == "nt" else "posix", []),
                         os.path.join(arch, "ffmpeg.pkg"), "ffmpeg")

    _set(state="extracting", percent=0, message="解压中…", speed=0)
    tmp = os.path.join(arch, "ffmpeg-unpack")
    shutil.rmtree(tmp, ignore_errors=True)
    _extract(pkg, tmp)

    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    exe = _find_file(tmp, name)
    if not exe:
        raise RuntimeError("压缩包里没找到 ffmpeg 可执行文件")
    ffprobe = _find_file(tmp, "ffprobe.exe" if os.name == "nt" else "ffprobe")

    final_bin = component_dir("ffmpeg")
    parent = os.path.dirname(exe)
    shutil.rmtree(os.path.dirname(final_bin), ignore_errors=True)
    os.makedirs(final_bin, exist_ok=True)
    if os.path.normpath(parent) != os.path.normpath(tmp):
        for f in os.listdir(parent):
            shutil.move(os.path.join(parent, f), os.path.join(final_bin, f))
    else:
        # 静态构建：二进制就在解压根目录，只搬需要的文件
        for f in (exe, ffprobe):
            if f and os.path.isfile(f):
                shutil.move(f, os.path.join(final_bin, os.path.basename(f)))
    shutil.rmtree(arch, ignore_errors=True)

    ver = _ffmpeg_version(os.path.join(final_bin, name))
    if not ver:
        raise RuntimeError("安装后 ffmpeg 无法运行（缺系统依赖？）")
    size = _dir_size(final_bin)
    _record("ffmpeg", version=ver, size=size, source="download")
    return {"version": ver, "size": size}


# ------------------------------------------------------------------ 安装：asr-libs
def _pip_cmd() -> Optional[List[str]]:
    """可用的 pip 命令；PyInstaller 单文件里没有 pip，返回 None。"""
    if getattr(sys, "frozen", False):
        return None
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "--version"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        if r.returncode == 0:
            return [sys.executable, "-m", "pip"]
    except Exception:
        pass
    return None


def _install_asr_libs(source: str) -> Dict[str, Any]:
    dest = component_dir("asr-libs")
    pip = _pip_cmd()
    if pip:
        # 开发 / 容器环境：直接 pip --target，与 requirements-asr.txt 同版本
        _set(state="installing", percent=0, speed=0,
             message="pip 安装中（约 170MB，需几分钟）…")
        cmd = pip + ["install", "--no-cache-dir", "--disable-pip-version-check",
                     "--upgrade", "--target", dest,
                     "faster-whisper==1.2.1", "ctranslate2==4.5.0"]
        if source == "mirror":
            cmd += ["-i", PYPI_MIRROR]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert proc.stdout is not None
        for raw in proc.stdout:
            _check_cancel()
            line = raw.decode("utf-8", "replace").strip()
            up = line.upper()
            if line and ("Collecting" in line or "Downloading" in line
                         or "Installing" in line or "Successfully" in line or "ERROR" in up):
                _log(line[:160])
        rc = proc.wait()
        _check_cancel()
        if rc != 0:
            raise RuntimeError("pip 安装失败（退出码 %d），详见日志；离线环境请用手动导入" % rc)
    else:
        # 打包版没有 pip：下载预打包 zip
        _set(state="downloading", percent=0, message="准备下载…", downloaded=0, total=0)
        cfg = load_config(refresh=True)
        urls = [str((cfg.get("ext") or {}).get("asr_libs_url") or "").strip()]
        urls += ASR_LIBS_URLS
        arch = os.path.join(ext_root(), "_tmp", "asr-libs.zip")
        pkg = _fetch_archive([u for u in urls if u], arch,
                             "语音识别依赖（预打包 zip 未发布时可手动导入）")
        _set(state="extracting", percent=0, message="解压中…", speed=0)
        shutil.rmtree(dest, ignore_errors=True)
        _extract(pkg, dest)
        shutil.rmtree(os.path.join(ext_root(), "_tmp"), ignore_errors=True)

    if not _has_pkg(dest, "faster_whisper"):
        raise RuntimeError("安装完成但没找到 faster_whisper 包，目录内容异常")
    ver = "faster-whisper 1.2.1"
    _record("asr-libs", version=ver, size=_dir_size(dest), source="pip" if pip else "zip")
    return {"version": ver, "size": _dir_size(dest)}


# ------------------------------------------------------------------ 安装：模型
def _install_model(size: str) -> Dict[str, Any]:
    repo = MODEL_REPOS.get(size)
    if not repo:
        raise RuntimeError("%s 暂无已验证的镜像源，请手动下载后用「手动导入」" % size)
    dest = component_dir("asr-model-%s" % size)
    os.makedirs(dest, exist_ok=True)
    for i, name in enumerate(MODEL_FILES):
        _check_cancel()
        target = os.path.join(dest, name)
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            _log("%s 已存在，跳过" % name)
            continue
        url = "https://modelscope.cn/models/%s/resolve/master/%s" % (repo, name)
        _set(state="downloading", percent=0, downloaded=0, total=0,
             message="下载 %s（%d/%d）" % (name, i + 1, len(MODEL_FILES)))
        _log("下载 " + url)
        _download(url, target)
    if not os.path.isfile(os.path.join(dest, "model.bin")):
        raise RuntimeError("模型下载不完整：缺少 model.bin")
    _record("asr-model-%s" % size, version=size, size=_dir_size(dest), source="modelscope")
    return {"version": size, "size": _dir_size(dest)}


# ------------------------------------------------------------------ 任务入口
def _install_metatube(source: str) -> Dict[str, Any]:
    """下载官方 Release（镜像优先），解压出单文件服务端并登记。"""
    from . import metatube
    if metatube.running():
        metatube.stop()
    dest = metatube.component_dir()
    os.makedirs(dest, exist_ok=True)
    tmp = os.path.join(ext_root(), "_tmp", "metatube.zip")
    _fetch_archive(metatube.asset_urls(), tmp, "MetaTube %s" % metatube.VERSION)
    work = os.path.join(ext_root(), "_tmp", "metatube-x")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    _extract(tmp, work)
    # 压缩包里的名字带平台后缀（metatube-server-windows-amd64.exe），按前缀找
    exe = ""
    for root, _dirs, files in os.walk(work):
        for f in files:
            lf = f.lower()
            if lf.startswith("metatube-server") and (lf.endswith(".exe") or "." not in lf):
                exe = os.path.join(root, f)
                break
        if exe:
            break
    if not exe:
        raise ValueError("压缩包里没有 MetaTube 可执行文件")
    final = metatube.binary_path()
    shutil.copy2(exe, final)
    if os.name != "nt":
        os.chmod(final, 0o755)
    ver = metatube.probe_version(final)
    if not ver:
        raise ValueError("MetaTube 无法运行（可能被杀软拦截，请加白名单后重试）")
    _record("metatube", version=ver, size=_dir_size(dest), source="ext")
    shutil.rmtree(work, ignore_errors=True)
    try:
        os.remove(tmp)
    except OSError:
        pass
    # 已启用 + 托管模式：装完顺手拉起，省一次点击
    try:
        cfgm = load_config().get("metatube") or {}
        if cfgm.get("enabled") and str(cfgm.get("mode")) == "managed":
            metatube.start()
    except Exception:
        pass
    return {"version": ver}


def install(cid: str, source: str = "auto", target: str = "") -> Dict[str, Any]:
    if cid not in ("ffmpeg", "asr-libs", "asr-model-small", "asr-model-medium", "metatube"):
        raise ValueError("未知组件：%s" % cid)
    with _LOCK:
        if _TASK.get("running"):
            raise ValueError("已有组件任务在进行中：%s" % _TASK.get("cid"))
        _TASK.clear()
        _TASK.update({
            "running": True, "cid": cid, "state": "prepare", "percent": 0,
            "message": "准备中…", "downloaded": 0, "total": 0, "speed": 0,
            "cancel": False, "log": [], "started": int(time.time()),
        })
    submit_task(_run_install, cid, str(source or "auto"))
    return {"ok": True}


def _run_install(cid: str, source: str) -> None:
    try:
        if cid == "ffmpeg":
            info = _install_ffmpeg(source)
        elif cid == "asr-libs":
            info = _install_asr_libs(source)
        elif cid == "metatube":
            info = _install_metatube(source)
        else:
            info = _install_model(cid.rsplit("-", 1)[-1])
        _set(running=False, state="done", percent=100,
             message="安装完成：%s" % (info.get("version") or ""), ended=int(time.time()))
        _log("安装完成")
    except _Cancelled:
        _set(running=False, state="cancelled", message="已取消", ended=int(time.time()))
    except Exception as e:                               # noqa: BLE001 任务层兜底
        _set(running=False, state="failed", message=str(e)[:300], ended=int(time.time()))
        _log("失败：" + str(e)[:160])


# ------------------------------------------------------------------ 导入 / 卸载
def import_component(cid: str, src: str) -> Dict[str, Any]:
    """手动导入：zip 包或已解压 / 已存在的目录。"""
    src = str(src or "").strip().strip('"')
    if not src or not os.path.exists(src):
        raise ValueError("路径不存在：%s" % src)
    if cid == "ffmpeg":
        name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        if zipfile.is_zipfile(src) or tarfile.is_tarfile(src):
            tmp = os.path.join(ext_root(), "_tmp", "ffmpeg-import")
            shutil.rmtree(tmp, ignore_errors=True)
            _extract(src, tmp)
            src = tmp
        exe = _find_file(src, name)
        probe = _find_file(src, "ffprobe.exe" if os.name == "nt" else "ffprobe")
        if not exe:
            raise ValueError("目录里没找到 ffmpeg 可执行文件")
        final_bin = component_dir("ffmpeg")
        shutil.rmtree(os.path.dirname(final_bin), ignore_errors=True)
        os.makedirs(final_bin, exist_ok=True)
        for f in (exe, probe):
            if f and os.path.isfile(f):
                shutil.copy2(f, os.path.join(final_bin, os.path.basename(f)))
        ver = _ffmpeg_version(os.path.join(final_bin, name))
        if not ver:
            raise ValueError("ffmpeg 无法运行（缺系统依赖？）")
        _record("ffmpeg", version=ver, size=_dir_size(final_bin), source="import")
        return {"version": ver}
    if cid == "metatube":
        from . import metatube
        if zipfile.is_zipfile(src) or tarfile.is_tarfile(src):
            tmp = os.path.join(ext_root(), "_tmp", "metatube-import")
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp, exist_ok=True)
            _extract(src, tmp)
            src = tmp
        exe = ""
        for root, _dirs, files in os.walk(src):
            for f in files:
                lf = f.lower()
                if lf.startswith("metatube-server") and (lf.endswith(".exe") or "." not in lf):
                    exe = os.path.join(root, f)
                    break
            if exe:
                break
        if not exe:
            raise ValueError("目录里没有 MetaTube 可执行文件")
        dest = metatube.component_dir()
        os.makedirs(dest, exist_ok=True)
        shutil.copy2(exe, metatube.binary_path())
        if os.name != "nt":
            os.chmod(metatube.binary_path(), 0o755)
        ver = metatube.probe_version(metatube.binary_path())
        if not ver:
            raise ValueError("MetaTube 无法运行（可能被杀软拦截）")
        _record("metatube", version=ver, size=_dir_size(dest), source="import")
        return {"version": ver}

    if cid == "asr-libs":
        if zipfile.is_zipfile(src):
            tmp = os.path.join(ext_root(), "_tmp", "asr-import")
            shutil.rmtree(tmp, ignore_errors=True)
            _extract(src, tmp)
            src = tmp
        # 允许给外层目录（一层以内找得到 faster_whisper 即可）
        root = src
        if not _has_pkg(root, "faster_whisper"):
            found = False
            for d in os.listdir(src) if os.path.isdir(src) else []:
                sub = os.path.join(src, d)
                if os.path.isdir(sub) and _has_pkg(sub, "faster_whisper"):
                    root, found = sub, True
                    break
            if not found:
                raise ValueError("目录里没找到 faster_whisper 包")
        dest = component_dir("asr-libs")
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(root, dest)
        _record("asr-libs", version="manual", size=_dir_size(dest), source="import")
        return {"version": "manual"}
    if cid.startswith("asr-model-"):
        size = cid.rsplit("-", 1)[-1]
        if os.path.isfile(src) and os.path.basename(src) == "model.bin":
            src = os.path.dirname(src)
        if not os.path.isfile(os.path.join(src, "model.bin")):
            raise ValueError("目录里没有 model.bin")
        dest = component_dir(cid)
        os.makedirs(dest, exist_ok=True)
        if os.path.normpath(src) != os.path.normpath(dest):
            for f in os.listdir(src):
                s, t = os.path.join(src, f), os.path.join(dest, f)
                if os.path.isfile(t) or not os.path.isfile(s):
                    continue
                # 大文件移动代替复制（同盘瞬时完成，跨盘也省一次写）
                if os.path.getsize(s) > 64 * 1048576:
                    shutil.move(s, t)
                else:
                    shutil.copy2(s, t)
        _record(cid, version=size, size=_dir_size(dest), source="import")
        return {"version": size}
    raise ValueError("未知组件：%s" % cid)


def uninstall(cid: str) -> Dict[str, Any]:
    if cid == "ffmpeg":
        shutil.rmtree(os.path.dirname(component_dir(cid)), ignore_errors=True)
    elif cid == "asr-libs":
        shutil.rmtree(component_dir(cid), ignore_errors=True)
    elif cid == "metatube":
        from . import metatube
        metatube.stop()
        shutil.rmtree(component_dir(cid), ignore_errors=True)
    elif cid.startswith("asr-model-"):
        # 只删默认位置的模型；用户自定义 model_dir 里的东西不属于组件系统管
        dest = component_dir(cid)
        default_root = os.path.join(str(DATA_DIR), "models")
        if os.path.normpath(dest).startswith(os.path.normpath(default_root)):
            shutil.rmtree(dest, ignore_errors=True)
        else:
            raise ValueError("该模型在自定义目录（asr.model_dir），请自行删除")
    else:
        raise ValueError("未知组件：%s" % cid)
    _forget(cid)
    return {"ok": True}
