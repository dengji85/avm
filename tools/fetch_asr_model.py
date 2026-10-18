# -*- coding: utf-8 -*-
"""下载语音对白提取所需模型（faster-whisper / CTranslate2 格式）。

为什么单独提供脚本而不是自动下载：
* 模型约 500MB，自动下载会卡住离线环境；
* 国内直连 HuggingFace 经常超时，这里走 ModelScope 镜像（实测可用）。

用法（在项目根目录）：
    python tools/fetch_asr_model.py                 # 默认 small → data/models/faster-whisper-small
    python tools/fetch_asr_model.py --size small
    python tools/fetch_asr_model.py --dir D:/models

下载完成后启动服务即可，详情页 AI 工作台会显示「提取对白」按钮。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import requests

# 已验证存在的 ModelScope 镜像；新增档位需要先确认镜像仓库存在
REPOS = {
    "small": "pengzhendong/faster-whisper-small",
}
FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")


def download(repo: str, name: str, dest_dir: str) -> str:
    url = "https://modelscope.cn/models/%s/resolve/master/%s" % (repo, name)
    dest = os.path.join(dest_dir, name)
    tmp = dest + ".part"
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        t0 = time.time()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if total and done % (64 * 1024 * 1024) < 1024 * 1024:
                    speed = done / 1024 / 1024 / max(0.1, time.time() - t0)
                    sys.stdout.write("\r  %s %5.1f%%  %.1f MB/s" % (name, done * 100.0 / total, speed))
                    sys.stdout.flush()
    sys.stdout.write("\r  %s 100%%\n" % name)
    os.replace(tmp, dest)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 faster-whisper 模型（ModelScope 镜像）")
    parser.add_argument("--size", default="small", help="模型档位（目前支持 small）")
    parser.add_argument("--dir", default="", help="模型目录，默认 data/models/faster-whisper-<size>")
    args = parser.parse_args()

    size = args.size.strip().lower()
    repo = REPOS.get(size)
    if not repo:
        print("不支持的档位：%s（可用：%s）" % (size, ", ".join(REPOS)))
        return 2

    root = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    dest_dir = args.dir.strip() or os.path.join(root, "data", "models", "faster-whisper-%s" % size)
    os.makedirs(dest_dir, exist_ok=True)
    print("模型：%s（%s）" % (repo, size))
    print("目录：%s" % dest_dir)

    for name in FILES:
        dest = os.path.join(dest_dir, name)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            print("  %s 已存在，跳过" % name)
            continue
        download(repo, name, dest_dir)

    need = os.path.join(dest_dir, "model.bin")
    print("完成：%s" % ("OK" if os.path.isfile(need) else "缺少 model.bin"))
    return 0 if os.path.isfile(need) else 1


if __name__ == "__main__":
    raise SystemExit(main())
