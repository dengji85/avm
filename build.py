# -*- coding: utf-8 -*-
"""
把「片匣」打包成 Windows 单文件可执行程序（--onefile）。

用法：
    python build.py

产物： dist/AVM.exe
- 双击即启动本地服务，打开一个控制台窗口，实时输出日志。
- 启动后自动打开浏览器访问 Web UI。
- 数据（library.db / covers / avatars / config.json）保存在 exe 同级的 data/ 目录，
  方便整体拷贝备份。
- 前端静态资源已打包进程序，无需额外文件。

停止：直接关闭控制台窗口，或按 Ctrl+C 优雅退出。
"""
import re
import os
import datetime
import PyInstaller.__main__

API_PY = os.path.join("app", "api.py")
BUILD_DATE_RE = re.compile(r'(BUILD_DATE\s*=\s*)"[^"]*"')
PLACEHOLDER = '"2026-08-16"'


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
    print("[build] BUILD_DATE 已写入: %s" % today)
    return src


def _restore_build_date(original_src):
    """还原 api.py，避免打包脚本污染工作区 / git。"""
    if original_src is None:
        return
    with open(API_PY, "w", encoding="utf-8") as f:
        f.write(original_src)
    print("[build] 已还原 %s" % API_PY)


_original_api = _rewrite_build_date()
try:
    PyInstaller.__main__.run([
    "run.py",
    "--name", "AVM",
    "--onefile",
    "--console",
    "--paths", ".",
    "--add-data", "web_dist;web_dist",
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
    "--clean",
    "--noconfirm",
])
finally:
    _restore_build_date(_original_api)
