# -*- coding: utf-8 -*-
"""把语音识别依赖打包成「一键安装」用的 zip。

为什么需要它：Windows 便携版 exe（PyInstaller 单文件）里没有 pip，
应用端只能「下载一个 zip → 解压到 data/ext/asr-libs」。这个脚本在能联网、
有 pip 的机器 / CI 上把依赖树一次性解析好并压成包，发布到 GitHub Releases。

产物（写入 --out，默认 dist/）：
    asr-libs-cp<pyver>-<platform>.zip          应用端 ASR_LIBS_URLS 指向的文件
    asr-libs-cp<pyver>-<platform>.zip.sha256   校验文件

用法：
    python tools/build_asr_zip.py               # 版本锁定读 requirements-asr.txt
    python tools/build_asr_zip.py --mirror      # 走清华 PyPI 镜像

说明：
* 产物只与 Python 版本 / 平台有关，与在哪台机器打包无关——
  请用与发布目标一致的 Python（应用是 PyInstaller 3.11 → 用 3.11 打包）
* 打包完会做导入冒烟测试（PYTHONPATH 指向产物 import faster_whisper / ctranslate2）
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / "requirements-asr.txt"
PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
SKIP_DIRS = {"__pycache__", ".pytest_cache"}


def py_tag():
    return "cp%d%d" % sys.version_info[:2]


def platform_tag():
    m = platform.machine().lower()
    if os.name == "nt":
        return "win64" if m in ("amd64", "x86_64") else "win-" + m
    return {"x86_64": "linux-x86_64", "amd64": "linux-x86_64",
            "aarch64": "linux-aarch64", "arm64": "linux-aarch64"}.get(m, "linux-" + m)


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd))
    # 子进程强制 UTF-8：requirements-asr.txt 是 UTF-8 + 中文注释，
    # Windows 默认 GBK 会让 pip 读文件时直接 UnicodeDecodeError
    env = kw.pop("env", None) or os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    subprocess.run([str(c) for c in cmd], check=True, env=env, **kw)


def build(out_dir, mirror):
    if not REQ.exists():
        raise SystemExit("找不到 %s" % REQ)
    src = out_dir / "asr-libs-src"
    shutil.rmtree(src, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir",
           "--disable-pip-version-check", "--target", src, "-r", REQ]
    if mirror:
        cmd += ["-i", PYPI_MIRROR]
    run(cmd)

    # 1) 目录完整性：关键包都在
    for pkg in ("faster_whisper", "ctranslate2"):
        if not (src / pkg).is_dir() and not list(src.glob(pkg + "-*.dist-info")):
            raise SystemExit("打包结果缺少 %s（pip 输出有误？）" % pkg)

    # 2) 导入冒烟测试：原生 DLL 真的能加载
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src)
    run([sys.executable, "-c",
         "import faster_whisper, ctranslate2; print('import ok')"], env=env)

    # 3) 压缩（跳过 __pycache__/*.pyc，能小 10%~20%）
    name = "asr-libs-%s-%s.zip" % (py_tag(), platform_tag())
    dest = out_dir / name
    if dest.exists():
        dest.unlink()
    n = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, dirnames, files in os.walk(src):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in files:
                if f.endswith((".pyc", ".pyo")):
                    continue
                p = Path(dirpath) / f
                zf.write(p, p.relative_to(src).as_posix())
                n += 1
    print("打包完成：%s（%d 个文件，%.1f MB）"
          % (dest.name, n, dest.stat().st_size / 1048576.0))

    # 4) SHA256
    h = hashlib.sha256(dest.read_bytes()).hexdigest()
    (out_dir / (name + ".sha256")).write_text("%s  %s\n" % (h, name), encoding="ascii")
    print("SHA256:", h)
    shutil.rmtree(src, ignore_errors=True)
    return dest


def main():
    ap = argparse.ArgumentParser(description="打包语音识别依赖 zip（供应用端一键安装下载）")
    ap.add_argument("--out", default=str(ROOT / "dist"), help="输出目录，默认 dist/")
    ap.add_argument("--mirror", action="store_true", help="pip 走清华 PyPI 镜像")
    args = ap.parse_args()
    dest = build(Path(args.out), args.mirror)
    print("")
    print("发布：把 %s 与 .sha256 一起传到 GitHub Release" % dest.name)
    print("（应用端 ASR_LIBS_URLS 已指向 releases/latest/download/%s）" % dest.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
