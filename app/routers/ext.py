# -*- coding: utf-8 -*-
"""扩展组件路由：状态 / 安装 / 离线导入 / 取消 / 自检 / 卸载。

从 api.py 拆出（阶段 2 试点）：逻辑都在 ext_components 里，这里只做参数与错误映射。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException

from .. import ext_components

router = APIRouter()


@router.get("/ext/status")
def ext_status() -> Dict[str, Any]:
    """全部可选组件（ffmpeg / ASR 依赖 / 模型）的检测状态与当前安装任务。"""
    from .. import ext_components
    return ext_components.status()


@router.get("/ext/task")
def ext_task() -> Dict[str, Any]:
    """当前安装任务进度（前端 2 秒轮询）。"""
    from .. import ext_components
    return ext_components.task_state()


@router.post("/ext/{cid}/install")
def ext_install(cid: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """安装组件：下载（可断点续传）→ 解压 → 校验。同一时间只允许一个任务。"""
    from .. import ext_components
    try:
        return ext_components.install(cid, source=str((payload or {}).get("source") or "auto"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e.args[0] if e.args else str(e)))


@router.post("/ext/task/cancel")
def ext_cancel() -> Dict[str, Any]:
    from .. import ext_components
    return ext_components.cancel()


@router.post("/ext/{cid}/import")
def ext_import(cid: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """手动导入离线包：zip / tar 包或已解压目录（服务端所在机器可访问的路径）。"""
    from .. import ext_components
    try:
        return ext_components.import_component(cid, str((payload or {}).get("dir") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e.args[0] if e.args else str(e)))


@router.post("/ext/{cid}/selftest")
def ext_selftest(cid: str) -> Dict[str, Any]:
    """组件自检：真实验证可用性并定位问题（路径 / 可运行 / 文件齐全）。"""
    from .. import ext_components
    try:
        return ext_components.selftest(cid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e.args[0] if e.args else str(e)))


@router.post("/ext/{cid}/uninstall")
def ext_uninstall(cid: str) -> Dict[str, Any]:
    from .. import ext_components
    try:
        return ext_components.uninstall(cid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e.args[0] if e.args else str(e)))
