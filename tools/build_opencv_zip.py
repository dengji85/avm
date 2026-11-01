# -*- coding: utf-8 -*-
"""把 opencv 运行库打包成「一键安装」用的 zip（人脸定位组件用）。

为什么需要它：Windows 便携版 exe（PyInstaller 单文件）里没有 pip，应用端只能
「下载一个 zip → 解压到 data/ext/facedet」。这个脚本在能联网、有 pip 的机器 / CI
上把 opencv-python-headless + numpy 一次性装好并压成包，发布到 GitHub Releases。

产物（写入 --out，默认 dist/）：
    opencv-libs-cp<pyver>-<platform>.zip          应用端 FACEDET_LIBS_URLS 指向的文件
    opencv-libs-cp<pyver>-<platform>.zip.sha256   校验文件

用法：
    python tools/build_opencv_zip.py
    python tools/build_opencv_zip.py --mirror

说明：
* 产物只与 Python 版本 / 平台有关——发布目标应用是 PyInstaller 3.11 → 用 3.11 打包
* 打包完做导入冒烟测试（PYTHONPATH 指向产物 import cv2 / numpy）
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
    env = kw.pop("env", None) or os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    subprocess.run([str(c) for c in cmd], check=True, env=env, **kw)


def build(out_dir, mirror):
    src = out_dir / "opencv-libs-src"
    shutil.rmtree(src, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir",
           "--disable-pip-version-check", "--target", src,
           "opencv-python-headless>=4.5.4", "numpy"]
    if mirror:
        cmd += ["-i", PYPI_MIRROR]
    run(cmd)

    for pkg in ("cv2", "numpy"):
        if not (src / pkg).is_dir() and not list(src.glob(pkg + "-*.dist-info")):
            raise SystemExit("打包结果缺少 %s（pip 输出有误？）" % pkg)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(src)
    run([sys.executable, "-c",
         "import cv2, numpy; print('import ok, opencv', cv2.__version__)"], env=env)

    name = "opencv-libs-%s-%s.zip" % (py_tag(), platform_tag())
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

    h = hashlib.sha256(dest.read_bytes()).hexdigest()
    (out_dir / (name + ".sha256")).write_text("%s  %s\n" % (h, name), encoding="ascii")
    print("SHA256:", h)
    shutil.rmtree(src, ignore_errors=True)
    return dest


def main():
    ap = argparse.ArgumentParser(description="打包 opencv 运行库 zip（人脸定位组件一键安装用）")
    ap.add_argument("--out", default=str(ROOT / "dist"), help="输出目录，默认 dist/")
    ap.add_argument("--mirror", action="store_true", help="pip 走清华 PyPI 镜像")
    args = ap.parse_args()
    dest = build(Path(args.out), args.mirror)
    print("")
    print("发布：把 %s 与 .sha256 一起传到 GitHub Release" % dest.name)
    print("（应用端 FACEDET_LIBS_URLS 已指向 releases/latest/download/%s）" % dest.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
