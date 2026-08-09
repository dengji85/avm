#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""(片匣 AVM) 统一入口。

既可以启动 Web 服务，也可以使用命令行子命令做脚本化操作：

    python run.py                         # 启动 Web 服务并打开浏览器
    python run.py --port 9000 --no-browser
    python run.py scan                    # 增量扫描媒体库
    python run.py scan --full             # 全量重扫
    python run.py dedupe                  # 列出重复组
    python run.py stats                   # 库规模与磁盘占用
    python run.py export --out lib.csv    # 导出 CSV
    python run.py organize --root D:/AV --dry-run
"""
import sys

from app.cli import main as cli_main

SUBCOMMANDS = {"scan", "dedupe", "stats", "export", "organize", "serve"}


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] not in SUBCOMMANDS:
        # 无论开发还是打包，默认都走控制台 serve 模式（有日志输出，Ctrl+C 退出）
        argv = ["serve"] + argv
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
