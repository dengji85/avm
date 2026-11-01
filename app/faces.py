# -*- coding: utf-8 -*-
"""封面主体定位：用 YuNet 找人脸，给 2:3 竖版海报挑一个不裁掉人物的窗口。

**为什么需要**：横版封面（约 1.5:1）裁成 2:3 要扔掉左右近一半画面，裁哪一侧
直接决定卡片上看到的是人物还是背景。早期只有「固定右侧 / 居中 / 补边」三档，
对构图不标准的封面（人物靠左、双人并排、竖版杂志图）必然裁歪。

**为什么用人脸而不是肤色/重心**：先在 28 张真实封面上试过肤色重心与滑窗方案，
结果经常选错——大面积的近景身体、男性侧脸都会把定位拽走，实测有两张图算出来的
位置与正确裁切方向相反。人脸检测准确得多，所以这里只保留人脸这一条路；
检测不到就老实返回 None，由调用方回退「右侧裁」，不会比原来更差。

依赖（两者都缺时功能自动降级，不影响其它任何流程）：
  * ``opencv-python-headless``：提供 ``cv2.FaceDetectorYN``（OpenCV ≥ 4.5.4；5.x 同样可用，
    注意 5.x 已移除旧的 ``CascadeClassifier``，所以不用 Haar）；
  * YuNet 模型 ``face_detection_yunet_2023mar.onnx``（227 KB）：首次调用时自动下载到
    ``data/models/``，走项目统一的下载通道（含代理与 TLS 配置）。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

_MODEL_NAME = "face_detection_yunet_2023mar.onnx"
#: 优先官方 raw（实测 jsdelivr 的 gh 镜像对大文件会回 131 字节的占位内容）
MODEL_URLS = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/" + _MODEL_NAME,
    "https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_detection_yunet/" + _MODEL_NAME,
)
_MODEL_MIN_BYTES = 100_000

#: 检测前把图缩到这个宽度（定位只要大致位置，缩图快一个数量级）
_WORK_W = 640
#: 人脸得分阈值：宁严勿滥，误检一张脸比没检测到更糟（会把窗口裁到路人身上）
_SCORE = 0.6

_LOCK = threading.Lock()
_DETECTOR: Any = None
_MODEL_FAIL_AT = 0.0
_MODEL_RETRY_SEC = 600.0   # 下载失败后 10 分钟内不再重试，避免每张图都卡在网络上
_MODEL_DOWNLOADING = False


def _tune(cv2: Any) -> Any:
    """OpenCV 5 的新图引擎不支持旧的 DNN target，每次建网络都会打一行 WARN 到
    stderr。告警对结果没有影响，压到 ERROR 级免得刷屏。"""
    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except Exception:
        pass
    return cv2


def _cv2() -> Any:
    """先找系统 / venv 里装好的 opencv，再尝试扩展组件目录（pip --target 装的）。

    扩展组件体系里 opencv 装到 ``data/ext/facedet/``，把该目录插到 sys.path
    就能 import——与语音识别依赖（asr-libs）是同一个模式。都没有时返回 None，
    智能裁切自动降级为「右侧裁」。
    """
    try:
        import cv2

        return _tune(cv2)
    except ImportError:
        pass
    try:
        from .ext_components import component_dir

        d = component_dir("facedet")
        if d and os.path.isdir(d) and d not in sys.path:
            sys.path.insert(0, d)
        import cv2

        return _tune(cv2)
    except Exception:
        return None


def model_path() -> Path:
    """模型文件的查找顺序：扩展组件目录 > data/models（旧位置，兼容升级）。

    组件目录是扩展组件页的下载 / 导入落点；旧位置保留只读兼容，避免已经下过
    模型的用户被要求重新下载。
    """
    try:
        from .ext_components import component_dir

        p = Path(component_dir("facedet")) / _MODEL_NAME
        if p.exists():
            return p
    except Exception:
        pass
    from .config import DATA_DIR

    return DATA_DIR / "models" / _MODEL_NAME


def _ensure_model() -> bool:
    """模型就位即返回 True；缺失时下载一次（失败进入冷却）。"""
    global _MODEL_FAIL_AT, _MODEL_DOWNLOADING

    p = model_path()
    try:
        if p.exists() and p.stat().st_size >= _MODEL_MIN_BYTES:
            return True
    except OSError:
        pass
    if time.monotonic() - _MODEL_FAIL_AT < _MODEL_RETRY_SEC:
        return False
    if _MODEL_DOWNLOADING:
        return False

    _MODEL_DOWNLOADING = True
    try:
        from . import images

        for url in MODEL_URLS:
            data = images.download(url, {})
            if data and len(data) >= _MODEL_MIN_BYTES:
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                    return True
                except OSError:
                    break
        _MODEL_FAIL_AT = time.monotonic()
        return False
    finally:
        _MODEL_DOWNLOADING = False


def _detector() -> Any:
    global _DETECTOR

    if _DETECTOR is not None:
        return _DETECTOR
    cv2 = _cv2()
    if cv2 is None or not hasattr(cv2, "FaceDetectorYN"):
        return None
    with _LOCK:
        if _DETECTOR is not None:
            return _DETECTOR
        if not _ensure_model():
            return None
        try:
            _DETECTOR = cv2.FaceDetectorYN.create(str(model_path()), "", (320, 320),
                                                  _SCORE, 0.3, 5000)
        except Exception:
            _DETECTOR = None
        return _DETECTOR


def available() -> bool:
    """是否具备人脸定位能力（opencv + 模型都就位）。会触发一次模型下载。"""
    return _detector() is not None


def _small(im: Any, max_w: int = _WORK_W) -> Any:
    if im.width <= max_w:
        return im
    return im.resize((max_w, max(1, round(im.height * max_w / im.width))))


def _pick_face(faces: Any, width: int) -> Optional[float]:
    """从检出结果里挑「最可能是封面主体」的那张脸，返回其水平中心（0–1）。

    先按面积从大到小；如果最大的那张贴在画面最边上（配图 / 路人群像），
    而还有更居中的脸，就选居中的那张——封面主体很少压在边缘。
    """
    items: List[Tuple[float, float]] = []
    for f in faces:
        try:
            fw = float(f[2])
            fh = float(f[3])
            cx = (float(f[0]) + fw / 2.0) / float(width)
        except (TypeError, ValueError, IndexError):
            continue
        items.append((fw * fh, cx))
    if not items:
        return None
    items.sort(key=lambda t: -t[0])
    for _area, cx in items:
        if 0.12 <= cx <= 0.88:
            return cx
    return items[0][1]


def face_center(im: Any) -> Optional[float]:
    """最大（或在合理位置最靠中间）人脸的的水平中心（0–1）；无能力/未检出返回 None。"""
    det = _detector()
    if det is None:
        return None
    try:
        import numpy as np

        img = _small(im)
        arr = np.asarray(img)
        bgr = np.ascontiguousarray(arr[:, :, ::-1])  # RGB -> BGR
        det.setInputSize((bgr.shape[1], bgr.shape[0]))
        _ret, faces = det.detect(bgr)
        if faces is None or len(faces) == 0:
            return None
        return _pick_face(faces, bgr.shape[1])
    except Exception:
        return None


def subject_center(im: Any) -> Tuple[Optional[float], str]:
    """返回 (水平中心 0–1, 来源)；来源取 ``face`` 或空串（判定不了）。"""
    cx = face_center(im)
    if cx is not None:
        return cx, "face"
    return None, ""


def status() -> dict:
    """给扩展组件页 / 设置页显示的能力状态（不触发下载）。"""
    cv2 = _cv2()
    has_dnn = bool(cv2 is not None and hasattr(cv2, "FaceDetectorYN"))
    p = model_path()
    has_model = False
    model_from = ""
    try:
        has_model = p.exists() and p.stat().st_size >= _MODEL_MIN_BYTES
        if has_model:
            try:
                from .ext_components import component_dir

                model_from = ("ext" if os.path.normpath(str(p.parent))
                              == os.path.normpath(component_dir("facedet")) else "legacy")
            except Exception:
                model_from = ""
    except OSError:
        pass
    ver = ""
    if cv2 is not None:
        ver = str(getattr(cv2, "__version__", "") or "")
    hint = ""
    if cv2 is None:
        hint = "missing_opencv"
    elif not has_dnn:
        hint = "old_opencv"
    elif not has_model:
        hint = "missing_model"
    return {
        "opencv": bool(cv2 is not None and has_dnn),
        "model": has_model,
        "model_path": str(p),
        "model_from": model_from,
        "version": ver,
        "ready": bool(has_dnn and has_model),
        "hint": hint,
    }
