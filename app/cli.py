# -*- coding: utf-8 -*-
"""(技术向) 命令行接口：可脚本化、可 cron、可无头运行。

示例：
    python run.py scan                 # 增量扫描
    python run.py scan --full          # 全量重扫（强制重新解析所有文件）
    python run.py dedupe --json        # 列出重复组（精确重复 + 同番号多版本）
    python run.py stats                # 库规模与磁盘占用
    python run.py export --out lib.csv # 导出 CSV
    python run.py organize --root D:/AV --dry-run   # 仅预览整理方案
    python run.py organize --root D:/AV --apply      # 执行整理（移动文件 + 更新库）
    python run.py serve                # 启动 Web 服务（等价于不带子命令）
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import threading
import webbrowser
from pathlib import Path, PurePosixPath

from .config import load_config
from .db import connect, init_db, query_all
from . import scanner, store, dedupe


_ILLEGAL = '\\/:*?"<>|'


def _safe(name: str) -> str:
    s = "".join("_" if c in _ILLEGAL else c for c in (name or "")).strip().strip(".")
    return s[:80] or "unknown"


def cmd_scan(args) -> int:
    init_db()
    res = scanner.run_scan(incremental=not args.full, workers=args.workers, hash_files=not args.no_hash)
    print(json.dumps(res, ensure_ascii=False))
    return 0


def cmd_dedupe(args) -> int:
    init_db()
    conn = connect()
    try:
        result = dedupe.scan(conn)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    print(f"精确重复组 : {result['exact_groups']} 组  (可清理 {result['exact_redundant']} 个冗余文件)")
    print(f"版本组     : {result['version_groups']} 组  (可合并 {result['version_redundant']} 个冗余文件)")
    for g in result["exact"]:
        print(f"\n  [精确重复] {g['size'] / 1024 ** 2:.1f} MB  hash={g['hash']}")
        for f in g["files"]:
            print(f"    - {f['path']}")
    for g in result["version"]:
        print(f"\n  [同番号多版本] {g['code']}  {g['title']}")
        for f in g["files"]:
            print(f"    - {f['resolution'] or '?'}  {f['path']}")
    return 0


def cmd_stats(args) -> int:
    init_db()
    conn = connect()
    try:
        s = store.stats(conn)
        st = store.storage_stats(conn)
        issues = store.integrity_issues(conn)
    finally:
        conn.close()
    print(f"影片   : {s['movies']}")
    print(f"文件   : {s['files']}")
    print(f"占用   : {s['size'] / 1024 ** 3:.1f} GB")
    print(f"已看   : {s['watched']}   收藏: {s['favorite']}   缺封面: {issues['missing_cover']}   未识别: {issues['unrecognized']}")
    print("\n按磁盘:")
    for d in st["by_disk"]:
        print(f"  {d['drive']:<6} {d['bytes'] / 1024 ** 3:8.1f} GB   {d['movies']:>6} 部   {d['files']:>7} 文件")
    print("\n按厂商 TOP:")
    for d in st["by_studio"][:8]:
        print(f"  {d['studio']:<16} {d['bytes'] / 1024 ** 3:8.1f} GB")
    return 0


def cmd_export(args) -> int:
    init_db()
    conn = connect()
    try:
        rows = store.find_movies(conn, {"page": 1, "page_size": 1000000})["items"]
    finally:
        conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["番号", "标题", "女优", "类型", "厂商", "系列", "发行日期", "时长", "大小(GB)", "目录"])
    for r in rows:
        w.writerow([
            r.get("display_code", ""), r.get("title", ""), " / ".join(r.get("actresses", [])),
            " / ".join(r.get("genres", [])), r.get("studio", ""), r.get("series", ""),
            r.get("release_date", ""), r.get("runtime", ""),
            round((r.get("size") or 0) / 1024 ** 3, 2), r.get("folder", ""),
        ])
    Path(args.out).write_text("\ufeff" + buf.getvalue(), encoding="utf-8")
    print(f"已导出 {len(rows)} 条 -> {args.out}")
    return 0


def _build_dst(template: str, m: dict, f: dict, root: str) -> Path:
    code = m.get("code") or "unknown"
    title = _safe(m.get("title") or code)
    studio = _safe(m.get("studio") or "Unknown")
    prefix = code.split("-")[0] if "-" in code else code
    ext = Path(f["path"]).suffix
    part = f.get("part") or 1
    suffix = f"-{part}" if part and part > 1 else ""
    dst_dir = template.format(studio=studio, code=code, title=title, prefix=prefix)
    dst_name = f"{code}{suffix}{ext}"
    return Path(root) / dst_dir / dst_name


def cmd_organize(args) -> int:
    init_db()
    conn = connect()
    try:
        movies = query_all(conn, "SELECT * FROM movies WHERE has_code = 1")
        plan = []
        for m in movies:
            files = query_all(conn, "SELECT * FROM movie_files WHERE movie_id = ? AND missing = 0", (m["id"],))
            for f in files:
                dst = _build_dst(args.template, m, f, args.root)
                if dst.exists():
                    continue
                if PurePosixPath(str(dst)) != PurePosixPath(f["path"]):
                    plan.append({"file_id": f["id"], "movie_id": m["id"],
                                 "src": f["path"], "dst": str(dst)})
    finally:
        conn.close()
    print(f"整理方案：{len(plan)} 个文件将移动到 {args.root}/{args.template}")
    for item in plan[:50]:
        print(f"  {item['src']}\n    -> {item['dst']}")
    if len(plan) > 50:
        print(f"  ... 其余 {len(plan) - 50} 项省略")
    if not args.apply:
        print("\n(这是 dry-run 预览，未做任何改动。加 --apply 才真正移动文件并更新库)")
        return 0
    conn = connect()
    try:
        moved = 0
        for item in plan:
            src, dst = Path(item["src"]), Path(item["dst"])
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.replace(dst)
                conn.execute("UPDATE movie_files SET path = ? WHERE id = ?", (str(dst), item["file_id"]))
                conn.execute("UPDATE movies SET folder = ? WHERE id = ?", (str(dst.parent), item["movie_id"]))
                moved += 1
            except Exception as e:
                print(f"移动失败 {src}: {e}", file=sys.stderr)
        conn.commit()
    finally:
        conn.close()
    print(f"\n已移动 {moved} 个文件并更新库。")
    return 0


def cmd_serve(args) -> int:
    from .main import app
    from .config import update_config
    import uvicorn
    app.state.bind_host = args.host
    # 将本次实际监听地址写回配置，供设置页/接口准确展示
    update_config({"server": {"host": args.host}})
    url = f"http://{args.host}:{args.port}/"
    print(f"片匣已启动： {url}")
    print("浏览器已自动打开；如需手动访问，复制上面的地址到浏览器。")
    print("按 Ctrl+C 退出服务。")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="avm", description="片匣 - 命令行工具")
    sub = p.add_subparsers(dest="cmd")
    ps = sub.add_parser("scan", help="扫描媒体库")
    ps.add_argument("--full", action="store_true", help="全量重扫（忽略增量缓存）")
    ps.add_argument("--no-hash", action="store_true", help="跳过内容指纹计算（更快，但无法去重）")
    ps.add_argument("--workers", type=int, default=None, help="并发线程数")
    sub.add_parser("dedupe", help="列出重复组").add_argument("--json", action="store_true")
    sub.add_parser("stats", help="库规模与磁盘占用")
    pe = sub.add_parser("export", help="导出 CSV")
    pe.add_argument("--out", default="av-museum.csv")
    po = sub.add_parser("organize", help="按模板整理文件（默认 dry-run）")
    po.add_argument("--root", required=True, help="整理目标根目录")
    po.add_argument("--template", default="{studio}/{code} {title}", help="目录模板，支持 {studio}/{code}/{title}/{prefix}")
    po.add_argument("--apply", action="store_true", help="真正移动文件（默认仅预览）")
    pv = sub.add_parser("serve", help="启动 Web 服务（控制台模式）")
    pv.add_argument("--host", default="127.0.0.1")
    pv.add_argument("--port", type=int, default=8770)
    pv.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    pv.add_argument("--reload", action="store_true", help="开发模式自动重载")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "scan":
        return cmd_scan(args)
    if args.cmd == "dedupe":
        return cmd_dedupe(args)
    if args.cmd == "stats":
        return cmd_stats(args)
    if args.cmd == "export":
        return cmd_export(args)
    if args.cmd == "organize":
        return cmd_organize(args)
    if args.cmd == "serve":
        return cmd_serve(args)
    # 默认行为：启动 Web 服务
    return cmd_serve(argparse.Namespace(host="127.0.0.1", port=8770))
