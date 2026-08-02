# -*- coding: utf-8 -*-
"""FastAPI 应用装配。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router
from .config import WEB_DIR, ensure_dirs
from .db import init_db

app = FastAPI(title="AV 博物馆 · 本地影片资料库", version=__version__, docs_url="/api/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.on_event("startup")
def _startup() -> None:
    ensure_dirs()
    init_db()


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
