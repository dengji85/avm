# -*- coding: utf-8 -*-
"""FastAPI 应用装配。"""
from __future__ import annotations

import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import Scope, Receive, Send
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router
from .config import WEB_DIR, ensure_dirs, DATA_DIR, CONFIG_PATH, load_config, ensure_access_token
from .db import init_db

app = FastAPI(title="片匣 · 本地 AV 收藏管理", version=__version__, docs_url="/api/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_LOCAL_ADDRS = {"127.0.0.1", "::1", "localhost", "testclient"}

# 路由判断：这些路径无需令牌即可访问（静态资源、首页、健康检查、接口文档）
_PUBLIC_PREFIXES = ("/assets", "/api/health", "/api/docs", "/api/openapi.json")


def _is_local(scope: Scope) -> bool:
    host = (scope.get("client") or ("", 0))[0] or ""
    return host in _LOCAL_ADDRS


def _token_valid(scope: Scope, cfg_token: str) -> bool:
    if not cfg_token:
        return True  # 未启用令牌，等同于关闭保护
    headers = scope.get("headers") or []
    hmap = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in headers}
    provided = hmap.get("x-access-token")
    if not provided:
        # 兼容 ?token=xxx
        qs = scope.get("query_string", b"").decode("latin-1")
        for pair in qs.split("&"):
            if pair.startswith("token="):
                provided = pair.split("=", 1)[1]
                break
    return provided == cfg_token


class AccessTokenMiddleware(BaseHTTPMiddleware):
    """远程访问强制带访问令牌；本机 127.0.0.1 与公开资源豁免。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith(_PUBLIC_PREFIXES) or path == "/" or path == "/index.html":
            return await call_next(request)
        cfg = load_config()
        if not cfg.get("server", {}).get("require_token_remote", True):
            return await call_next(request)
        if _is_local(request.scope):
            return await call_next(request)
        tok = (cfg.get("server", {}).get("access_token") or "").strip()
        if _token_valid(request.scope, tok):
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"detail": "需要访问令牌", "code": "NO_TOKEN"},
        )


app.add_middleware(AccessTokenMiddleware)
app.include_router(router)


def _backup_database() -> None:
    """启动时为 library.db 留一份每日备份，防止升级/误删丢数据。
    仅保留最近 7 份，避免无限增长。"""
    try:
        from pathlib import Path
        import shutil
        import datetime as _dt
        db = DATA_DIR / "library.db"
        if not db.exists():
            return
        stamp = _dt.date.today().strftime("%Y%m%d")
        bak = DATA_DIR / f"library.db.bak{stamp}"
        if not bak.exists():
            shutil.copy2(db, bak)
        # 清理超过 7 天的旧备份
        for old in DATA_DIR.glob("library.db.bak*"):
            try:
                days = (_dt.date.today() - _dt.date.fromtimestamp(old.stat().st_mtime)).days
                if days > 7:
                    old.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _record_version() -> None:
    """把当前程序版本写入 config.json，便于升级时识别与提示。"""
    try:
        data = {}
        if CONFIG_PATH.exists():
            try:
                data = __import__("json").loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        if data.get("app_version") != __version__:
            data["app_version"] = __version__
            CONFIG_PATH.write_text(
                __import__("json").dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception:
        pass


@app.on_event("startup")
def _startup() -> None:
    ensure_dirs()
    init_db()
    _backup_database()
    _record_version()
    tok = ensure_access_token()
    cfg = load_config()
    host = cfg.get("server", {}).get("host", "127.0.0.1")
    if host in ("0.0.0.0", ""):
        print("=" * 48)
        print("远程访问已开启，局域网设备需携带访问令牌：")
        print("  " + tok)
        print("前端设置页可查看/重置令牌；本机 127.0.0.1 访问免令牌。")
        print("=" * 48)
    _start_auto_scan()


def _start_auto_scan() -> None:
    """后台定时增量扫描：按 library.auto_scan_interval（分钟）轮询媒体库。

    复用 scanner.run_scan(incremental=True) 的增量能力（仅处理新增/变更文件），
    与手动扫描共享 SCAN 锁，避免并发写库冲突。间隔为 0 时不启动。
    """
    import threading

    from . import scanner
    from .api import SCAN

    def _loop() -> None:
        while True:
            interval = int(load_config().get("library", {}).get("auto_scan_interval", 0) or 0)
            if interval <= 0:
                # 未启用：睡眠较长后重试（配置可能在运行时被打开）
                time.sleep(60)
                continue
            # 睡眠间隔（秒），按分钟换算
            time.sleep(interval * 60)
            try:
                if SCAN.running:
                    continue  # 手动扫描进行中，跳过本轮
                if not (load_config().get("library", {}).get("paths") or []):
                    continue  # 还没配置扫描目录
                if not SCAN.start():
                    continue
                def _run():
                    try:
                        scanner.run_scan(
                            progress_cb=SCAN.update,
                            incremental=True,
                            auto_cleanup=True,
                        )
                        SCAN.finish("自动扫描完成")
                    except Exception as e:  # noqa: BLE001
                        SCAN.error(str(e))
                        SCAN.finish("自动扫描失败：" + str(e))
                threading.Thread(target=_run, daemon=True).start()
            except Exception:  # noqa: BLE001
                try:
                    SCAN.cancel()
                except Exception:
                    pass

    t = threading.Thread(target=_loop, name="auto-scan", daemon=True)
    t.start()


@app.exception_handler(Exception)
async def _unhandled(request, exc):  # pragma: no cover
    return JSONResponse({"detail": f"服务内部错误：{exc}"}, status_code=500)


@app.get("/")
def index() -> FileResponse:
    # index.html 不缓存，确保前端更新后立即生效（避免用户看到旧版）
    return FileResponse(
        WEB_DIR / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# Vite 构建产物：模块脚本与静态资源位于 /assets 下
# 产物文件名自带 contenthash，可安全长缓存；用自定义 StaticFiles 复用相同策略。
class HashedStaticFiles(StaticFiles):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        # 带 hash 的资源视为不可变，长期缓存；其余不缓存
        if "-" in path.split("/")[-1]:
            resp.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        else:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/assets", HashedStaticFiles(directory=str(WEB_DIR / "assets")), name="assets")
