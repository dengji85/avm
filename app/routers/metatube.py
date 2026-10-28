# -*- coding: utf-8 -*-
"""MetaTube 刮削引擎的运维端点（状态 / 安装 / 启停 / 连接测试）。

只做「服务本身」的事；真正参与刮削是通过 providers/metatube.py 接入调度层的。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException

router = APIRouter()


@router.get("/metatube/status")
def metatube_status() -> Dict[str, Any]:
    """托管状态：是否已装、是否在跑、目标版本、当前安装任务。"""
    from .. import metatube
    return metatube.status()


@router.get("/metatube/task")
def metatube_task() -> Dict[str, Any]:
    """安装（下载）任务进度，前端 1.5s 轮询。"""
    from .. import metatube
    return metatube.task_state()


@router.post("/metatube/install")
def metatube_install() -> Dict[str, Any]:
    """下载官方二进制并解压到 data/ext/metatube。"""
    from .. import metatube
    try:
        return metatube.install()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e.args[0] if e.args else str(e)))


@router.post("/metatube/start")
def metatube_start() -> Dict[str, Any]:
    """拉起托管进程（幂等）。"""
    from .. import metatube
    res = metatube.start()
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "启动失败")
    return res


@router.post("/metatube/stop")
def metatube_stop() -> Dict[str, Any]:
    from .. import metatube
    return metatube.stop()


@router.post("/metatube/test")
def metatube_test(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """连接测试：可临时传入 base_url / token，不落盘。"""
    from .. import metatube
    return metatube.health(
        base_url=str((payload or {}).get("base_url") or "").strip(),
        token=str((payload or {}).get("token") or "").strip(),
    )


@router.get("/metatube/providers")
def metatube_providers() -> Dict[str, Any]:
    """转发服务端的 /v1/providers，用于设置页展示「实际启用了哪些源」。"""
    from .. import metatube
    try:
        return metatube.providers()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
