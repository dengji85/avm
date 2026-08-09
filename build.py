# -*- coding: utf-8 -*-
"""
把「片匣」打包成 Windows 单文件夹可执行程序。

用法：
    python build.py

产物： dist/AVM/AVM.exe
- 双击即启动本地服务，打开一个控制台窗口，实时输出日志。
- 启动后自动打开浏览器访问 Web UI。
- 数据（library.db / covers / avatars / config.json）保存在 exe 同级的 data/ 目录，
  方便整体拷贝备份。
- 前端静态资源已打包进程序，无需额外文件。

停止：直接关闭控制台窗口，或按 Ctrl+C 优雅退出。
"""
import PyInstaller.__main__

PyInstaller.__main__.run([
    "run.py",
    "--name", "AVM",
    "--onedir",
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
