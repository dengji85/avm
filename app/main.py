# -*- coding: utf-8 -*-
"""FastAPI 应用装配。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router
from .config import WEB_DIR, ensure_dirs, DATA_DIR, CONFIG_PATH
from .db import init_db

app = FastAPI(title="片匣 · 本地 AV 收藏管理", version=__version__, docs_url="/api/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
