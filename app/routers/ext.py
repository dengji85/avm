# -*- coding: utf-8 -*-
"""扩展组件路由：状态 / 安装 / 离线导入 / 取消 / 自检 / 卸载。

从 api.py 拆出（阶段 2 试点）：逻辑都在 ext_components 里，这里只做参数与错误映射。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException, Request, UploadFile

from .. import ext_components

router = APIRouter()


def _save_upload(file: UploadFile) -> str:
    """把上传的离线包落到临时目录，返回服务器路径（供导入函数使用）。

    NAS / Docker 场景的三个约束：
      - 峰值空间约为包体积的 3 倍（上传包 + 解压 + 安装副本），先预检；
      - 大文件按 1MB 分块写盘，不会一次读进内存；
      - 每次上传顺手清掉 24 小时前的残留包，避免 NAS 空间被慢慢吃光。
    """
    import os
    import re
    import shutil
    import time

    name = os.path.basename(file.filename or "upload.zip")
    if not name.lower().endswith((".zip", ".tar.gz", ".tgz", ".tar")):
        raise HTTPException(status_code=400, detail="仅支持 .zip / .tar.gz / .tgz / .tar 离线包")
    name = re.sub(r"[^\w.\-]", "_", name)
    updir = os.path.join(ext_components.ext_root(), "_tmp", "uploads")
    os.makedirs(updir, exist_ok=True)
    # 清理 24 小时前的残留（导入正常结束时由调用方立即删，这里兜底异常中断）
    now = time.time()
    for old in os.listdir(updir):
        p = os.path.join(updir, old)
        try:
            if os.path.isfile(p) and now - os.path.getmtime(p) > 24 * 3600:
                os.remove(p)
        except OSError:
            pass
    # 空间预检：NAS 的数据卷常常不大，写一半才失败会留下难查的半成品。
    # UploadFile.size 在部分 FastAPI 版本里取不到，所以不依赖它：
    # 已知大小按 3 倍（包体 / 解压 / 安装副本）估算，未知时至少要求 512MB。
    try:
        free = shutil.disk_usage(updir).free
        size = int(getattr(file, "size", 0) or 0)
        need = max(size * 3, 512 * 1024 * 1024)
        if free < need:
            raise HTTPException(
                status_code=400,
                detail="磁盘剩余空间不足：需要约 %d MB，可用 %d MB（离线包需包体 / 解压 / 安装三份空间）"
                       % (need // (1024 * 1024), free // (1024 * 1024)))
    except OSError:
        pass
    dst = os.path.join(updir, "%d-%s" % (int(now * 1000), name))
    try:
        with open(dst, "wb") as f:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    except Exception:
        try: os.remove(dst)
        except OSError: pass
        raise
    return dst


def _cleanup_upload(path: str) -> None:
    """导入结束后立即释放上传包占用的空间（NAS 上尤其重要）。"""
    import os
    try: os.remove(path)
    except OSError: pass


@router.get("/ext/packages")
def ext_packages(request: Request, dir: str = "") -> Dict[str, Any]:
    """列出 AVM 服务端可访问的离线包，供「免上传」导入选择。

    浏览器拿不到本地文件的真实路径，所以「选择文件但不上传」的等价做法是：
    让服务端列出它自己能读到的包（默认 Downloads 与 ext 临时目录，也可指定
    NAS 共享目录），用户选中后走 /import，文件完全不经网络传输。

    ``same_host`` 仅用于文案：由请求来源 IP 判断浏览器与 AVM 是否在同一台机器
    （反代时取 X-Forwarded-For 首段）。即使不同机，列表里也是「服务端目录」，
    NAS 上把包放进共享目录同样能免上传导入。
    """
    import os
    from pathlib import Path

    host = (request.client.host if request.client else "") or ""
    xff = str(request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    effective = xff or host
    same_host = effective in ("127.0.0.1", "::1", "localhost")

    # 默认离线包目录：随 ext 目录（可配）走，首次访问自动创建，
    # 用户只要把 .zip 放进去就能在界面里直接选，无需上传、无需填路径。
    default_dir = os.path.join(ext_components.ext_root(), "packages")
    try:
        os.makedirs(default_dir, exist_ok=True)
    except OSError:
        default_dir = ""
    candidates = []
    if dir.strip():
        candidates.append(dir.strip().strip('"'))
    else:
        if default_dir:
            candidates.append(default_dir)
        candidates.append(os.path.join(ext_components.ext_root(), "_tmp", "uploads"))
        try:
            candidates.append(str(Path.home() / "Downloads"))
        except OSError:
            pass
        candidates.append(ext_components.ext_root())

    groups = []
    for d in dict.fromkeys(candidates):
        try:
            if not os.path.isdir(d):
                continue
            files = []
            for name in os.listdir(d):
                if not name.lower().endswith((".zip", ".tar.gz", ".tgz", ".tar")):
                    continue
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    files.append({"name": name, "path": p,
                                  "size": os.path.getsize(p)})
            if files:
                groups.append({"dir": d, "files": sorted(files, key=lambda x: x["name"])[:50]})
        except OSError:
            continue
    return {"same_host": same_host, "default_dir": default_dir, "groups": groups}


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


# 注意：必须在 /ext/{cid}/import **之前**注册——否则 bundle 会被当成 cid 吃掉
@router.post("/ext/bundle/import")
def ext_bundle_import(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """导入整合离线包（tools/build_ext_bundle.py 的产物）：按 manifest 一次装齐所有组件。

    进度走 /ext/task 的任务通道（cid='bundle'）。单组件失败不中断其余组件。
    """
    from .. import ext_components

    try:
        return ext_components.import_bundle(str((payload or {}).get("dir") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e.args[0] if e.args else str(e)))


@router.post("/ext/{cid}/import")
def ext_import(cid: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """手动导入离线包：zip / tar 包或已解压目录（服务端所在机器可访问的路径）。"""
    from .. import ext_components
    try:
        return ext_components.import_component(cid, str((payload or {}).get("dir") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e.args[0] if e.args else str(e)))


@router.post("/ext/bundle/upload")
async def ext_bundle_upload(file: UploadFile) -> Dict[str, Any]:
    """上传整合离线包并导入（服务端所在机器与浏览器不是同一台时也能用）。

    注意：import_bundle 会启动**后台任务**并立即返回，这里不能删上传包；
    由 ext_components 在任务结束时清理（见 _maybe_remove_uploaded_bundle）。
    """
    from .. import ext_components
    path = _save_upload(file)
    try:
        return ext_components.import_bundle(path)
    except ValueError as e:
        _cleanup_upload(path)   # 启动失败（已有任务 / 缺 manifest）才当场清理
        raise HTTPException(status_code=400, detail=str(e.args[0] if e.args else str(e)))


@router.post("/ext/{cid}/upload")
async def ext_upload(cid: str, file: UploadFile) -> Dict[str, Any]:
    """上传单组件离线包（zip / tar）并导入。"""
    from .. import ext_components
    path = _save_upload(file)
    try:
        return ext_components.import_component(cid, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e.args[0] if e.args else str(e)))
    finally:
        _cleanup_upload(path)


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
