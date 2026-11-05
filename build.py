# -*- coding: utf-8 -*-
"""
把「片匣」打包成 Windows 单文件可执行程序（--onefile）。

用法：
    python build.py

产物： dist/AVM-<版本号>.exe（如 AVM-1.12.0.exe）
- 双击即启动本地服务，打开一个控制台窗口，实时输出日志。
- 启动后自动打开浏览器访问 Web UI。
- 数据（library.db / covers / avatars / config.json）保存在 exe 同级的 data/ 目录，
  方便整体拷贝备份。
- 前端静态资源已打包进程序，无需额外文件。

停止：直接关闭控制台窗口，或按 Ctrl+C 优雅退出。
"""
import re
import os
import sys
import datetime
import shutil
import PyInstaller.__main__

# UPX 能把 onefile 产物再压一截，但它不是 Python 依赖，需要系统单独安装。
# 显式探测：装了就传给 PyInstaller，没装就 --noupx，避免不同机器上
# 「有时压缩有时不压缩」导致产物体积不可预期。
_upx = shutil.which("upx")
if _upx:
    _upx_args = ["--upx-dir", os.path.dirname(_upx)]
    print("[build] UPX detected, compression enabled: %s" % _upx)
else:
    _upx_args = ["--noupx"]
    print("[build] UPX not found, compression skipped")

# Windows CI 的控制台默认可能是 cp1252 等非 UTF-8 编码，脚本里的中文 print 会抛
# UnicodeEncodeError。这里把标准输出统一切到 UTF-8（环境不支持时静默跳过）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

API_PY = os.path.join("app", "api.py")
BUILD_DATE_RE = re.compile(r'(BUILD_DATE\s*=\s*)"[^"]*"')
PLACEHOLDER = '"2026-08-16"'

VERSION_RE = re.compile(r'__version__\s*=\s*"([^"]+)"')
with open(os.path.join("app", "__init__.py"), "r", encoding="utf-8") as _f:
    _ver = VERSION_RE.search(_f.read())
APP_VERSION = _ver.group(1) if _ver else "dev"
DIST_NAME = "AVM-%s" % APP_VERSION


def _rewrite_build_date():
    """打包时把 BUILD_DATE 常量重写成本地构建当天日期，并返回原内容以便还原。"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    with open(API_PY, "r", encoding="utf-8") as f:
        src = f.read()
    if not BUILD_DATE_RE.search(src):
        return None
    new_src = BUILD_DATE_RE.sub(r'\g<1>"%s"' % today, src, count=1)
    with open(API_PY, "w", encoding="utf-8") as f:
        f.write(new_src)
    # 输出保持纯 ASCII：CI（尤其 Windows runner）控制台编码可能是 cp1252，
    # 打印中文会抛 UnicodeEncodeError 直接中断构建。
    print("[build] BUILD_DATE written: %s" % today)
    return src


def _restore_build_date(original_src):
    """还原 api.py，避免打包脚本污染工作区 / git。"""
    if original_src is None:
        return
    with open(API_PY, "w", encoding="utf-8") as f:
        f.write(original_src)
    print("[build] restored %s" % API_PY)


_original_api = _rewrite_build_date()
try:
    PyInstaller.__main__.run([
    "run.py",
    "--name", DIST_NAME,
    "--onefile",
    "--console",
    "--paths", ".",
    "--add-data", "web_dist;web_dist",
    # 竖版海报智能裁切的 YuNet 人脸模型（227KB）：封面裁切是核心体验，随包内置，
    # 不依赖首次联网下载。faces.model_path 会按「组件目录 > 内置 > data/models」查找。
    "--add-data", os.path.join("app", "assets", "face_detection_yunet_2023mar.onnx") + ";assets",
    # opencv 运行库有 50MB+，不值得让所有人为「还没用上的功能」买单：
    # 不打进安装包，需要智能裁切的用户去 扩展组件 → 人脸定位 一键安装（打包版
    # 下载预打包 zip、源码版 pip --target）。没装时 faces.py 自动退回右侧裁。
    "--exclude-module", "cv2",
    # numpy 只为 cv2 服务（faces.face_center 推理用），全项目再无使用者：
    # 随 opencv 组件一起装进组件目录，不占安装包体积。
    "--exclude-module", "numpy",
    "--collect-submodules", "app",
    "--hidden-import", "uvicorn.logging",
    "--hidden-import", "uvicorn.lifespan.on",
    "--hidden-import", "uvicorn.protocols.http.auto",
    "--hidden-import", "uvicorn.protocols.websockets.auto",
    "--hidden-import", "uvicorn.loops.auto",
    "--hidden-import", "uvicorn.workers",
    "--hidden-import", "multipart",
    "--hidden-import", "email.mime.text",
    "--hidden-import", "email.mime.multipart",
    "--hidden-import", "charset_normalizer",
    # 语音对白提取是可选组件（依赖约 170MB + 模型 460MB），
    # 绝不打进主程序：用户需要时另装，用 AVM_ASR_LIBS 指向即可。
    "--exclude-module", "faster_whisper",
    "--exclude-module", "ctranslate2",
    "--exclude-module", "onnxruntime",
    "--exclude-module", "tokenizers",
    "--exclude-module", "huggingface_hub",
    "--exclude-module", "hf_xet",
    "--exclude-module", "av",
    "--clean",
    "--noconfirm",
    *_upx_args,
])
finally:
    _restore_build_date(_original_api)
