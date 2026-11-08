# -*- coding: utf-8 -*-
"""(技术向) 命令行接口：可脚本化、可 cron、可无头运行。

示例：
    python run.py scan                 # 增量扫描
    python run.py scan --full          # 全量重扫（强制重新解析所有文件）
    python run.py dedupe --json        # 列出重复组（精确重复 + 同番号多版本）
    python run.py stats                # 库规模与磁盘占用
    python run.py export --out lib.csv # 导出 CSV
    python run.py organize --root D:/AV --dry-run                # 仅预览整理方案
    python run.py organize --root D:/AV --apply                  # 执行整理（移动文件 + 更新库）
    python run.py organize --root D:/AV --mode hardlink --apply  # 硬链接整理（同卷零拷贝），失败自动降级复制
    python run.py serve                # 启动 Web 服务（等价于不带子命令）
"""
from __future__ import annotations

import argparse
import csv
import errno
import io
import json
import os
import shutil
import sys
import threading
import webbrowser
from pathlib import Path, PurePosixPath

from .config import load_config
from .db import connect, init_db, query_all
from . import scanner, store, dedupe


_ILLEGAL = '\\/:*?"<>|'


def _safe(name: str) -> str:
    # strip 必须放在截断之后：[:80] 可能恰好停在空格处，而 Windows 不允许
    # 目录名以空格结尾（mkdir 会被系统剥掉，后续按带空格路径操作就找不到了）
    s = "".join("_" if c in _ILLEGAL else c for c in (name or ""))
    return s[:80].strip().strip(".") or "unknown"


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


_ORGANIZE_MODES = ("move", "copy", "hardlink", "symlink")
_ORGANIZE_VERBS = {"move": "移动", "copy": "复制", "hardlink": "硬链接", "symlink": "软链接"}


def _place_file(mode: str, src: Path, dst: Path) -> str:
    """按整理模式放置单个文件，返回实际生效的模式。

    链接模式建链失败（hardlink 跨盘、Windows 未开开发者模式 / 非管理员）时
    自动降级为复制，保证批量任务不因个别文件中断。
    """
    if mode == "move":
        src.replace(dst)
        return "move"
    if mode == "copy":
        shutil.copy2(src, dst)
        return "copy"
    try:
        if mode == "hardlink":
            os.link(src, dst)
        else:
            os.symlink(src, dst)
        return mode
    except OSError as e:
        # EXDEV=跨盘硬链；EPERM/EACCES/winerror 1314=Windows 建链权限不足
        if getattr(e, "errno", None) in (errno.EXDEV, errno.EPERM, errno.EACCES) \
                or getattr(e, "winerror", None) == 1314:
            shutil.copy2(src, dst)
            return "copy"
        raise


def cmd_organize(args) -> int:
    init_db()
    mode = args.mode
    conn = connect()
    try:
        movies = query_all(conn, "SELECT * FROM movies WHERE has_code = 1")
        plan = []
        for m in movies:
            files = query_all(conn, "SELECT * FROM movie_files WHERE movie_id = ? AND missing = 0 "
                                    "AND COALESCE(kind,'local') <> 'online'", (m["id"],))
            for f in files:
                dst = _build_dst(args.template, m, f, args.root)
                if dst.exists():
                    continue
                if PurePosixPath(str(dst)) != PurePosixPath(f["path"]):
                    plan.append({"file_id": f["id"], "movie_id": m["id"],
                                 "src": f["path"], "dst": str(dst)})
    finally:
        conn.close()
    verb = _ORGANIZE_VERBS[mode]
    print(f"整理方案：{len(plan)} 个文件将{verb}到 {args.root}/{args.template}")
    for item in plan[:50]:
        print(f"  {item['src']}\n    -> {item['dst']}")
    if len(plan) > 50:
        print(f"  ... 其余 {len(plan) - 50} 项省略")
    db_note = "并更新库" if mode == "move" else "，库中路径仍指向原文件"
    if not args.apply:
        print(f"\n(这是 dry-run 预览，未做任何改动。加 --apply 才真正{verb}文件{db_note})")
        return 0
    conn = connect()
    done = 0
    fallback = 0
    try:
        for item in plan:
            src, dst = Path(item["src"]), Path(item["dst"])
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                actual = _place_file(mode, src, dst)
                if actual == "copy" and mode != "copy":
                    fallback += 1
                if mode == "move":
                    conn.execute("UPDATE movie_files SET path = ? WHERE id = ?", (str(dst), item["file_id"]))
                    conn.execute("UPDATE movies SET folder = ? WHERE id = ?", (str(dst.parent), item["movie_id"]))
                done += 1
            except Exception as e:
                print(f"{verb}失败 {src}: {e}", file=sys.stderr)
        conn.commit()
    finally:
        conn.close()
    print(f"\n已{verb} {done} 个文件。")
    if mode == "move":
        print("库中路径已同步更新。")
    else:
        print("仅 move 模式会改库；库中路径仍指向原文件。")
    if fallback:
        print(f"其中 {fallback} 个无法建链（跨盘硬链 / Windows 权限不足），已降级为复制。"
              "软链接需开启 Windows 开发者模式或以管理员运行。")
    return 0


def cmd_serve(args) -> int:
    from .main import app
    from .config import update_config, ensure_access_token, load_config, DATA_DIR
    import uvicorn
    app.state.bind_host = args.host
    # 将本次实际监听地址写回配置，供设置页/接口准确展示
    update_config({"server": {"host": args.host, "port": args.port}})
    protect = True
    try:
        protect = bool(load_config().get("server", {}).get("token_required", True))
    except Exception:
        protect = True
    # 展示用地址：0.0.0.0 在浏览器里打不开，统一显示回环地址
    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    url = f"http://{shown_host}:{args.port}/"
    print(f"片匣已启动： {url}")
    print("浏览器已自动打开；如需手动访问，复制上面的地址到浏览器。")
    # 本机也不再免令牌：完整令牌写在文件里（0600），日志只打印掩码
    try:
        print(f"数据目录： {DATA_DIR}")
        if protect:
            from .config import TOKEN_FILE, mask_token
            print(f"访问令牌： {mask_token(ensure_access_token())}（完整值见 {TOKEN_FILE}）")
        else:
            print("⚠ 访问保护已关闭：任何能连上的设备都可直接访问")
    except Exception:
        pass
    print("按 Ctrl+C 退出服务。")
    if not args.no_browser:
        def _open() -> None:
            """用一次性短链打开浏览器：本机也要令牌，但体验保持无感。

            令牌不放进 URL（会进浏览器历史 / 反代日志），改用 5 分钟一次性的 ?c=。
            --reload 时服务跑在子进程里，父进程生成的 code 无效，故直接开裸地址。
            """
            target = url
            if protect and not getattr(args, "reload", False):
                try:
                    from . import auth
                    target = f"{url}?c={auth.new_login_link()}"
                except Exception:
                    target = url
            webbrowser.open(target)
        threading.Timer(1.5, _open).start()
    # proxy_headers：让 uvicorn 依据可信代理（server.trusted_proxies）还原真实客户端 IP；
    # 否则同机反向代理时所有请求来源都会是 127.0.0.1，访问控制会被整体绕过
    # （详见 docs/安全与隐私设计.md）。
    try:
        _trusted = ",".join(
            str(x) for x in (load_config().get("server", {}).get("trusted_proxies") or [])
        ) or "127.0.0.1,::1"
    except Exception:
        _trusted = "127.0.0.1,::1"
    # 注意：uvicorn 的自动重载只在传入「导入字符串」时生效，传 app 对象会静默退化为
    # 不重载（只在 stderr 留一条 WARNING）。所以 --reload 走 "app.main:app"，
    # reloader 会在子进程里重新导入模块，模块级初始化照样执行。
    if args.reload:
        # 只监听代码目录。默认监听整个工作目录时，data/ 下的 library.db、访问令牌、
        # 日志、封面任一变化都会触发整进程重启 —— 一边用一边刮削/改设置就会不断重启，
        # 而 Windows 上子进程若没有可用的控制台句柄，reloader 发 CTRL_C_EVENT 会直接
        # 以 `OSError: [WinError 6] 句柄无效` 崩掉整个服务（实测可复现）。
        _root = Path(__file__).resolve().parent.parent
        _dirs = [d for d in (_root / "app", _root / "web_src" / "src") if d.is_dir()]
        uvicorn.run("app.main:app", host=args.host, port=args.port, reload=True,
                    reload_dirs=[str(d) for d in _dirs] or None,
                    log_level="info", proxy_headers=True, forwarded_allow_ips=_trusted)
    else:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info",
                    proxy_headers=True, forwarded_allow_ips=_trusted)
    return 0


def cmd_ai_check(args) -> int:
    """诊断助手链路：模型配置 / 库字典 / 这句话实际被解析成什么、命中几条。

    「为什么查不出」通常只有三种原因：没配模型、库里写法对不上、模型没吐结构化参数。
    这条命令把三者一次打印出来，不用猜。
    """
    init_db()
    from . import agent as agent_mod, vocab as vocab_mod

    cfg = load_config(refresh=True)
    ai = cfg.get("ai") or {}
    print("AI 启用 :", "是" if ai.get("enabled") else "否")
    print("端点    :", ai.get("base_url") or "(未填)")
    print("模型    :", ai.get("model") or "(未填)")

    conn = connect()
    try:
        block = vocab_mod.block(conn)
    finally:
        conn.close()
    print("\n库字典（前 600 字）:")
    print(block[:600] + ("…" if len(block) > 600 else ""))

    if not ai.get("enabled"):
        print("\n→ 模型未启用：助手只能用本地规则，跨语言对应（丝袜 → パンスト）不会生效。")
        return 1

    print("\n问句:", args.question)
    print("调模型中…")
    try:
        res = agent_mod.run_agent(args.question, cfg, max_steps=max(1, int(args.steps or 2)))
    except Exception as e:                                   # noqa: BLE001 诊断命令要打印而不是抛栈
        print("调用失败:", e)
        return 2

    if res.get("fallback"):
        print("注意: 端点不支持 tools，已回退到单步规划")
    for i, s in enumerate(res.get("steps") or [], 1):
        print("  第%d步 %-14s %s  (%s ms)"
              % (i, s.get("name"), s.get("summary"), s.get("ms")))
    items = res.get("items") or []
    print("命中:", len(items), "条")
    for m in items[:5]:
        print("   -", m.get("code") or "", m.get("title") or "")
    if res.get("rows"):
        for r in res["rows"]:
            print("   ·", r.get("key"), "=", r.get("value"))
    if res.get("note"):
        print("理解成:", res["note"])
    if res.get("answer"):
        print("模型结论:", res["answer"])
    if res.get("stopped") == "max_steps":
        print("（达到步数上限，未给出结论）")
    return 0 if (items or res.get("rows")) else 1


def cmd_web_check(args) -> int:
    """联网搜索自检：验证搜索后端配置，并真实搜索一次给示例。"""
    from . import websearch

    cfg = load_config(refresh=True)
    kind, value = websearch.backend(cfg)
    if not kind:
        print(value)
        return 1
    print("后端 :", ("SearXNG (%s)" % value) if kind == "searxng" else "Tavily")
    print("搜索 :", args.query)
    res = websearch.check(cfg, args.query)
    print("结果 :", "OK" if res.get("ok") else "失败")
    print("     ", res.get("message"))
    if res.get("ok"):
        try:
            for r in websearch.search(cfg, args.query, limit=3):
                print("   ·", (r.get("title") or "")[:60], "|", (r.get("url") or "")[:70])
        except Exception as e:                       # noqa: BLE001 自检命令要打印而不是抛栈
            print("   列举失败:", e)
    return 0 if res.get("ok") else 2


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
    po.add_argument("--mode", choices=_ORGANIZE_MODES, default="move",
                    help="放置方式：move=移动并更新库（默认）/ copy=复制 / hardlink=硬链接（同卷零拷贝）"
                         "/ symlink=软链接；建链失败（跨盘 / Windows 权限）自动降级为复制")
    po.add_argument("--apply", action="store_true", help="真正执行（默认仅预览）")
    sub.add_parser("mcp", help="以 MCP 服务器（stdio）运行，供外部 AI 客户端连接（需 pip install mcp）")
    pa = sub.add_parser("ai-check", help="诊断 AI 助手：模型配置 / 库字典 / 这句话会被解析成什么")
    pa.add_argument("question", nargs="?", default="找一下穿丝袜的、年纪大一点的", help="要诊断的问句")
    pa.add_argument("--steps", type=int, default=4, help="最多让 agent 走几轮（每轮可能调多个工具）")
    pw = sub.add_parser("web-check", help="联网搜索自检：验证搜索后端并真实搜索一次")
    pw.add_argument("query", nargs="?", default="test", help="测试搜索词")
    pv = sub.add_parser("serve", help="启动 Web 服务（控制台模式）")
    # 默认值支持环境变量：容器 / NAS 下用 AVM_HOST=0.0.0.0 直接对外监听
    pv.add_argument("--host", default=os.environ.get("AVM_HOST", "127.0.0.1"))
    pv.add_argument("--port", type=int, default=int(os.environ.get("AVM_PORT", "8770")))
    pv.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    pv.add_argument("--reload", action="store_true", help="开发模式自动重载")
    return p


def _force_utf8_stdio() -> None:
    """把标准输出 / 错误统一切到 UTF-8（环境不支持时静默跳过）。

    Windows 控制台以及「重定向到文件」的场景默认走本地编码（cp936/cp1252 等），
    中文日志会以本地编码写出，之后用 UTF-8 工具打开就成了一片乱码。
    开发脚本、打包 exe、容器都以本函数所在模块为唯一入口，故在此统一兜底。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None) -> int:
    _force_utf8_stdio()
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
    if args.cmd == "mcp":
        from .mcp_server import run as run_mcp
        return run_mcp()
    if args.cmd == "ai-check":
        return cmd_ai_check(args)
    if args.cmd == "web-check":
        return cmd_web_check(args)
    if args.cmd == "serve":
        return cmd_serve(args)
    # 默认行为：启动 Web 服务
    return cmd_serve(argparse.Namespace(
        host=os.environ.get("AVM_HOST", "127.0.0.1"),
        port=int(os.environ.get("AVM_PORT", "8770")),
    ))
