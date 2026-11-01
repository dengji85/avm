# -*- coding: utf-8 -*-
"""把所有扩展组件打成「整合离线包」，一次下载、一次导入。

为什么需要它：在线安装的各组件分散在 gyan.dev / GitHub / ModelScope，国内直连
慢且不稳。整合包一次下载全带上，扩展组件页导入即可。

产物（写入 --out，默认 dist/）：
    AVM-ext-bundle-<platform>-<date>.zip
    AVM-ext-bundle-<platform>-<date>.zip.sha256

zip 结构（由 manifest.json 描述，应用端 import_bundle 据此分流）：
    manifest.json
    ffmpeg/ffmpeg.exe, ffprobe.exe
    asr-libs/…        （faster_whisper / ctranslate2，pip --target 产物）
    opencv-libs/…     （cv2 / numpy）
    metatube/metatube-server-windows-amd64.exe
    models/asr-model-small/…   （可选，--with-model，约 460MB）

用法：
    python tools/build_ext_bundle.py                 # 不含语音模型
    python tools/build_ext_bundle.py --with-model    # 连 small 模型一起（约 700MB）
    python tools/build_ext_bundle.py --skip-download # 只用 dist/ 里已有的 zip 组装
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import metatube                      # noqa: E402
from app.ext_components import (              # noqa: E402
    FFMPEG_SOURCES, _download, _extract, _find_file,
)

GH_MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/", "")
PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
SKIP_DIRS = {"__pycache__", ".pytest_cache"}
WORK = ROOT / "dist" / "_bundle_work"


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd))
    env = kw.pop("env", None) or os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    subprocess.run([str(c) for c in cmd], check=True, env=env, **kw)


def gh_mirror_urls(url: str):
    """GitHub 直连对国内不友好：镜像前缀优先、直连兜底。"""
    out = [m + url for m in GH_MIRRORS]
    return out


def fetch(url: str, dest: Path, what: str) -> Path:
    last = ""
    for u in (gh_mirror_urls(url) if "github.com" in url else [url]):
        try:
            print("下载", u[:100])
            _download(u, str(dest))
            if dest.stat().st_size > 1000:
                return dest
            print("  源返回内容异常（%d 字节），换下一个源" % dest.stat().st_size)
        except Exception as e:                           # noqa: BLE001
            last = str(e)
            print("  源失败：", last[:140])
    raise SystemExit("%s 所有下载源都失败了：%s" % (what, last[:200]))


def py_tag():
    return "cp%d%d" % sys.version_info[:2]


def plat_tag():
    m = platform.machine().lower()
    if os.name == "nt":
        return "win64" if m in ("amd64", "x86_64") else "win-" + m
    return {"x86_64": "linux-x86_64", "amd64": "linux-x86_64",
            "aarch64": "linux-aarch64", "arm64": "linux-aarch64"}.get(m, "linux-" + m)


def pip_target(target: Path, pkgs, mirror: bool):
    shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir",
           "--disable-pip-version-check", "--target", str(target), *pkgs]
    if mirror:
        cmd += ["-i", PYPI_MIRROR]
    run(cmd)


def main():
    ap = argparse.ArgumentParser(description="打包扩展组件整合离线包")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--mirror", action="store_true", help="pip 走清华 PyPI 镜像")
    ap.add_argument("--with-model", action="store_true",
                    help="连语音识别 small 模型一起（约 +460MB）")
    ap.add_argument("--force-download", action="store_true",
                    help="忽略本地已有的 ffmpeg / metatube，强制在线下载")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    arch = out / "_bundle_pkg"
    arch.mkdir(parents=True, exist_ok=True)

    comps: dict = {}

    # ---- ffmpeg（优先复用本地的：组件目录 / PATH / tools 便携包；找不到才在线下）----
    print("\n== ffmpeg ==")
    from app.ext_components import ffmpeg_exe

    fdir = WORK / "ffmpeg"
    shutil.rmtree(fdir, ignore_errors=True)
    fdir.mkdir(parents=True)
    sources = FFMPEG_SOURCES.get("nt" if os.name == "nt" else "posix", [])
    pkg = arch / "ffmpeg.pkg"
    local_ff = ffmpeg_exe()
    if local_ff and os.path.isfile(local_ff) and not args.force_download:
        probe = os.path.join(os.path.dirname(local_ff),
                             "ffprobe.exe" if os.name == "nt" else "ffprobe")
        shutil.copy2(local_ff, fdir / Path(local_ff).name)
        if os.path.isfile(probe):
            shutil.copy2(probe, fdir / Path(probe).name)
        print("  复用本地 ffmpeg:", local_ff)
    else:
        done = False
        for u in sources:
            try:
                fetch(u, pkg, "ffmpeg")
                tmp = WORK / "_ffmpeg_x"
                shutil.rmtree(tmp, ignore_errors=True)
                _extract(str(pkg), str(tmp))
                name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
                exe = _find_file(str(tmp), name)
                if not exe:
                    raise RuntimeError("包里没找到 ffmpeg 可执行文件")
                probe = _find_file(str(tmp), "ffprobe.exe" if os.name == "nt" else "ffprobe")
                shutil.copy2(exe, fdir / Path(exe).name)
                if probe:
                    shutil.copy2(probe, fdir / Path(probe).name)
                done = True
                break
            except SystemExit:
                raise
            except Exception as e:                       # noqa: BLE001
                print("  ffmpeg 源失败：", str(e)[:140])
        if not done:
            raise SystemExit("ffmpeg 全部下载源失败")
    comps["ffmpeg"] = {"dir": "ffmpeg", "version": ""}

    # ---- asr-libs（优先复用 dist 里已发布的 zip）----
    print("\n== asr-libs ==")
    adir = WORK / "asr-libs"
    shutil.rmtree(adir, ignore_errors=True)
    asr_zip = out / ("asr-libs-%s-%s.zip" % (py_tag(), plat_tag()))
    if asr_zip.is_file():
        _extract(str(asr_zip), str(adir))
        print("  复用已打好的", asr_zip.name)
    else:
        pip_target(adir, ["faster-whisper==1.2.1", "ctranslate2==4.5.0"], args.mirror)
    comps["asr-libs"] = {"dir": "asr-libs", "version": "faster-whisper 1.2.1"}

    # ---- opencv（人脸定位运行库）----
    print("\n== opencv-libs ==")
    odir = WORK / "opencv-libs"
    shutil.rmtree(odir, ignore_errors=True)
    oc_zip = out / ("opencv-libs-%s-%s.zip" % (py_tag(), plat_tag()))
    if oc_zip.is_file():
        _extract(str(oc_zip), str(odir))
        print("  复用已打好的", oc_zip.name)
    else:
        pip_target(odir, ["opencv-python-headless>=4.5.4", "numpy"], args.mirror)
    comps["facedet"] = {"dir": "opencv-libs", "version": "opencv-libs"}

    # ---- metatube（优先复用本地组件目录里的二进制；没有才镜像下载）----
    print("\n== metatube ==")
    mdir = WORK / "metatube"
    shutil.rmtree(mdir, ignore_errors=True)
    mdir.mkdir(parents=True)
    mpkg = arch / "metatube.pkg"
    local_mt = metatube.binary_path()
    if os.path.isfile(local_mt) and not args.force_download:
        shutil.copy2(local_mt, mdir / os.path.basename(local_mt))
        print("  复用本地 metatube:", local_mt)
    else:
        fetch(metatube.asset_urls()[0], mpkg, "metatube")
        tmp = WORK / "_metatube_x"
        shutil.rmtree(tmp, ignore_errors=True)
        _extract(str(mpkg), str(tmp))
        exe = ""
        for root, _dirs, files in os.walk(tmp):
            for f in files:
                lf = f.lower()
                if lf.startswith("metatube-server") and (lf.endswith(".exe") or "." not in lf):
                    exe = os.path.join(root, f)
                    break
            if exe:
                break
        if not exe:
            raise SystemExit("metatube 包里没找到可执行文件")
        shutil.copy2(exe, mdir / os.path.basename(exe))
    comps["metatube"] = {"dir": "metatube", "version": metatube.VERSION}

    # ---- 可选：语音模型 ----
    if args.with_model:
        print("\n== asr-model-small（ModelScope）==")
        mdir2 = WORK / "models" / "asr-model-small"
        mdir2.mkdir(parents=True, exist_ok=True)
        repo = "pengzhendong/faster-whisper-small"
        for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
            t = mdir2 / name
            if t.is_file() and t.stat().st_size > 0:
                continue
            fetch("https://modelscope.cn/models/%s/resolve/master/%s" % (repo, name),
                  t, "model %s" % name)
        comps["asr-model-small"] = {"dir": "models/asr-model-small", "version": "small"}

    # ---- manifest + 压缩 ----
    manifest = {
        "bundle": 1,
        "created": datetime.date.today().isoformat(),
        "platform": "%s-%s" % (plat_tag(), py_tag()),
        "components": comps,
    }
    (WORK / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    date = datetime.date.today().strftime("%Y%m%d")
    dest = out / ("AVM-ext-bundle-%s-%s.zip" % (plat_tag(), date))
    if dest.exists():
        dest.unlink()
    print("\n== 压缩 %s ==" % dest.name)
    n = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, dirnames, files in os.walk(WORK):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in files:
                if f.endswith((".pyc", ".pyo")):
                    continue
                p = Path(dirpath) / f
                zf.write(p, p.relative_to(WORK).as_posix())
                n += 1
    h = hashlib.sha256(dest.read_bytes()).hexdigest()

    # 指针文件：组件更新时只替换 Release 里的 zip 与这个 json，应用端无需改代码。
    # sha256 写在这里——GitHub 资产详情页虽有 digest，但应用端自动下载时用 json 对照更直接。
    info = {
        "file": dest.name,
        "sha256": h,
        "size": dest.stat().st_size,
        "date": manifest["created"],
        "platform": manifest["platform"],
    }
    (out / "bundle-latest.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    print("完成：%s（%d 个文件，%.1f MB）" % (dest.name, n, dest.stat().st_size / 1048576.0))
    print("SHA256:", h)
    print("\n发布（一次上传，长期有效——放 ext-bundle 固定 tag，与应用发版解耦）：")
    print("  1. %s" % dest.name)
    print("  2. bundle-latest.json   ← 指针；以后组件更新只替换 zip 和它，应用不用动")
    print("  首次创建：Releases → New release → tag 填 ext-bundle")
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.rmtree(arch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
