# -*- coding: utf-8 -*-
"""语音对白提取（ASR）：只服务「AI 更懂剧情」，不产出外挂字幕。

为什么不做成字幕：
* 这类片大量段落是喘息/拟声/背景音，Whisper 会把它们识别成语义不通的碎片；
  直接写进 SRT 只会得到一半噪声，还要用户逐条删。
* 对 AI 剧情总结来说，只要「对白文本 + 时间点」就够了，噪声必须过滤掉。

所以本模块的目标是「**可读的对白摘要文本**」：
  data/asr/{movie_id}/dialog.txt   [mm:ss] 对白
  data/asr/{movie_id}/meta.json    模型 / 时长 / 段数 / 字数 / 耗时

依赖是可选的：没装 faster-whisper 时接口会返回明确提示，其他 AI 功能不受影响。
模型不自动下载（离线环境会卡住），放到 data/models/faster-whisper-{size} 即可。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import DATA_DIR, load_config
from .db import connect
from .threadpool import submit_task

LOG_PREFIX = "[asr]"

# 过滤阈值（宁可少几行，也不要给模型喂幻觉）
NO_SPEECH_MAX = 0.6        # 静音概率高于此值 → 不是人声
LOGPROB_MIN = -1.0         # 平均对数概率过低 → 识别不可信
COMPRESS_MAX = 2.4         # 压缩比过高 → 重复/胡言乱语
MIN_TEXT_LEN = 4           # 太短的碎片（「あ」「ん」）对剧情没有价值
MAX_LINE_SECONDS = 20.0    # 单段过长说明模型在连续编，截断

_JOBS: Dict[int, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_MODEL_CACHE: Dict[Tuple[str, str, str], Any] = {}
_MODEL_LOCK = threading.Lock()

_CLOCK = re.compile(r"^\s*\[(\d+):(\d{2})\]")


def _asr_cfg(refresh: bool = False) -> Dict[str, Any]:
    return (load_config(refresh=refresh) or {}).get("asr") or {}


def libs_dir(refresh: bool = False) -> str:
    """依赖目录：环境变量优先，其次是设置页里配的 asr.libs_dir。"""
    env = os.environ.get("AVM_ASR_LIBS", "").strip()
    if env:
        return env
    return str(_asr_cfg(refresh).get("libs_dir") or "").strip()


def _ensure_libs() -> None:
    """把外部依赖目录加进 sys.path（不污染项目环境，便于放到 D 盘/组件包）。"""
    extra = libs_dir()
    if extra and os.path.isdir(extra) and extra not in sys.path:
        sys.path.insert(0, extra)


def lib_available() -> bool:
    _ensure_libs()
    return importlib.util.find_spec("faster_whisper") is not None


def model_root(size: str) -> str:
    """模型目录：环境变量 > 设置页 asr.model_dir > 默认 data/models。

    允许两种填法：模型根目录（下面有 faster-whisper-small）或直接指向模型目录。
    """
    env = os.environ.get("AVM_ASR_MODEL_DIR", "").strip()
    custom = env or str(_asr_cfg().get("model_dir") or "").strip()
    if custom:
        if os.path.isfile(os.path.join(custom, "model.bin")):
            return custom
        return os.path.join(custom, "faster-whisper-%s" % size)
    return str(DATA_DIR / "models" / ("faster-whisper-%s" % size))


def model_available(size: str) -> bool:
    root = model_root(size)
    return os.path.isfile(os.path.join(root, "model.bin"))


def status() -> Dict[str, Any]:
    """全局能力探测：前端据此决定是否显示「提取对白」。"""
    # 设置页刚改过路径时，这里必须读新值
    cfg = load_config(refresh=True)
    a = (cfg.get("asr") or {})
    size = str(a.get("model") or "small")
    return {
        "lib_available": lib_available(),
        "model_available": model_available(size),
        "model": size,
        "model_dir": model_root(size),
        "libs_dir": libs_dir(),
        "language": str(a.get("language") or "ja"),
    }


def _dialog_dir(movie_id: int) -> str:
    d = os.path.join(str(DATA_DIR), "asr", str(movie_id))
    os.makedirs(d, exist_ok=True)
    return d


def _dialog_path(movie_id: int) -> str:
    return os.path.join(_dialog_dir(movie_id), "dialog.txt")


def _meta_path(movie_id: int) -> str:
    return os.path.join(_dialog_dir(movie_id), "meta.json")


def read_meta(movie_id: int) -> Dict[str, Any]:
    try:
        with open(_meta_path(movie_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def dialog_text(movie_id: int) -> str:
    """给 AI 用的对白文本；没有提取过返回空串。"""
    if not read_meta(movie_id):
        return ""
    try:
        with open(_dialog_path(movie_id), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def summary(movie_id: int, preview: int = 0) -> Dict[str, Any]:
    """给前端的状态：任务进度 / 已完成的统计 / 可选预览文本。"""
    with _JOBS_LOCK:
        job = dict(_JOBS.get(movie_id) or {})
    meta = read_meta(movie_id)
    out: Dict[str, Any] = {
        "status": job.get("status") or meta.get("status") or "idle",
        "percent": job.get("percent", 100 if meta else 0),
        "message": job.get("message", ""),
        "error": job.get("error") or meta.get("error") or "",
        "segments": meta.get("segments", 0),
        "chars": meta.get("chars", 0),
        "model": meta.get("model", ""),
        "language": meta.get("language", ""),
        "elapsed": meta.get("elapsed", 0),
        "created_at": meta.get("created_at", ""),
    }
    if preview > 0:
        text = dialog_text(movie_id)
        out["text"] = text[:preview]
        out["has_text"] = bool(text)
    return out


def clear(movie_id: int) -> None:
    with _JOBS_LOCK:
        _JOBS.pop(movie_id, None)
    for p in (_dialog_path(movie_id), _meta_path(movie_id)):
        try:
            os.remove(p)
        except OSError:
            pass


def _set_job(movie_id: int, **kw: Any) -> None:
    with _JOBS_LOCK:
        job = _JOBS.setdefault(movie_id, {})
        job.update(kw)


def _job_cancelled(movie_id: int) -> bool:
    with _JOBS_LOCK:
        return bool((_JOBS.get(movie_id) or {}).get("cancel"))


def _load_model(size: str, device: str):
    _ensure_libs()
    from faster_whisper import WhisperModel  # 延迟导入：没装依赖时其他功能不受影响

    key = (model_root(size), device, size)
    with _MODEL_LOCK:
        hit = _MODEL_CACHE.get(key)
        if hit is not None:
            return hit
        root = model_root(size)
        if os.path.isdir(root) and os.path.isfile(os.path.join(root, "model.bin")):
            model = WhisperModel(root, device=device, compute_type="int8",
                                 cpu_threads=min(8, (os.cpu_count() or 4)))
        else:
            # 允许直接写模型名，交给 faster-whisper 自己找缓存（首次会联网下载）
            model = WhisperModel(size, device=device, compute_type="int8",
                                 cpu_threads=min(8, (os.cpu_count() or 4)))
        _MODEL_CACHE[key] = model
        return model


def _clock(sec: float) -> str:
    sec = max(0, int(sec or 0))
    return "%d:%02d" % (sec // 60, sec % 60)


_ONLY_KANA = re.compile(r"^[\sぁ-んァ-ヶーｰ、。,.!?！？…~〜\-]+$")


_KANA_KANJI = re.compile(r"[ぁ-んァ-ヶー一-龯]")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")


def _lang_ok(text: str, language: str) -> bool:
    """语言一致性：固定 ja 时，纯数字/英文/乱码行一律丢弃。

    实测在无对白的片头，模型会输出「2.5 cm.」「3.5 cm.」这类幻觉；
    如果语言已固定为日语，要求出现假名或汉字是最省事也最有效的过滤。
    """
    lang = (language or "").split("-")[0].lower()
    if lang in ("ja", "jp"):
        return bool(_KANA_KANJI.search(text))
    if lang in ("zh", "cn"):
        return bool(_CJK.search(text))
    if lang in ("en", "eng"):
        return bool(_LATIN.search(text))
    return True


def _keep(seg: Any, text: str, last: str, language: str = "") -> bool:
    """判断一段识别结果是否值得保留：宁可少，也不要给模型喂幻觉。"""
    t = (text or "").strip()
    if len(t) < MIN_TEXT_LEN:
        return False
    if getattr(seg, "no_speech_prob", 0) and seg.no_speech_prob > NO_SPEECH_MAX:
        return False
    lp = getattr(seg, "avg_logprob", None)
    if lp is not None and lp < LOGPROB_MIN:
        return False
    cr = getattr(seg, "compression_ratio", None)
    if cr is not None and cr > COMPRESS_MAX:
        return False
    if (seg.end - seg.start) > MAX_LINE_SECONDS:
        return False
    if not _lang_ok(t, language):
        return False
    # 纯假名短句多为喘息/拟声（「あぁ」「んっ」），对剧情感知没有帮助
    if _ONLY_KANA.match(t) and len(t) <= 8:
        return False
    # 字符种类过少（「ああああ」「いいいい」）
    if len(set(t)) <= 2:
        return False
    # 数字/符号占比过高（读数、时间码、标尺）
    digits = sum(ch.isdigit() for ch in t)
    letters = sum(ch.isalpha() for ch in t)
    if digits and digits >= max(2, letters):
        return False
    # 与上一句几乎重复
    if last:
        a, b = set(last), set(t)
        overlap = len(a & b) / max(1, len(a | b))
        if overlap > 0.9:
            return False
    return True


def _prepare_audio(video: str, out_wav: str, ffmpeg: str,
                   max_seconds: int = 0, start_seconds: int = 0) -> float:
    """抽 16k 单声道 wav；返回音频时长（秒）。"""
    cmd = [ffmpeg, "-y", "-v", "error"]
    if start_seconds > 0:
        cmd += ["-ss", str(int(start_seconds))]
    if max_seconds > 0:
        cmd += ["-t", str(int(max_seconds))]
    cmd += ["-i", video, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out_wav]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not os.path.isfile(out_wav):
        err = (proc.stderr or b"").decode("utf-8", "replace")[:300]
        raise RuntimeError("抽取音轨失败：%s" % (err or "ffmpeg 返回非零退出码"))
    return os.path.getsize(out_wav) / (16000 * 2.0)


def _run(movie_id: int, video: str, cfg: Dict[str, Any], size: str,
         language: str, max_seconds: int, start_seconds: int = 0) -> None:
    started = time.time()
    wav = os.path.join(_dialog_dir(movie_id), "_audio.wav")
    try:
        ffmpeg = str(cfg.get("ffmpeg_path") or "ffmpeg").strip() or "ffmpeg"
        _set_job(movie_id, status="running", percent=2, message="抽取音轨…", error="")
        duration = _prepare_audio(video, wav, ffmpeg, max_seconds, start_seconds)
        if _job_cancelled(movie_id):
            raise RuntimeError("已取消")

        _set_job(movie_id, percent=8, message="加载语音模型…")
        model = _load_model(size, "cpu")
        if _job_cancelled(movie_id):
            raise RuntimeError("已取消")

        _set_job(movie_id, percent=12, message="识别对白…")
        # 注意：不用内置 vad_filter——它依赖 onnxruntime，在部分环境会直接崩溃；
        # 噪声交给 _keep() 在文本层面过滤，效果等价且稳定。
        segments, info = model.transcribe(
            wav,
            language=None if language in ("", "auto") else language,
            beam_size=5,
            vad_filter=False,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            log_prob_threshold=LOGPROB_MIN,
            compression_ratio_threshold=COMPRESS_MAX,
        )
        total = float(info.duration or duration or 0)
        lines: List[str] = []
        last = ""
        kept = chars = 0
        for seg in segments:
            if _job_cancelled(movie_id):
                raise RuntimeError("已取消")
            text = (seg.text or "").strip()
            if _keep(seg, text, last, language):
                lines.append("[%s] %s" % (_clock(seg.start), text))
                last = text
                kept += 1
                chars += len(text)
            pct = 12 + int(83 * min(1.0, (seg.end / total) if total else 0))
            _set_job(movie_id, percent=pct, message="已识别 %d 段" % kept)

        text = "\n".join(lines)
        if not text:
            raise RuntimeError("没有识别到可用对白：这段音频可能以环境音/拟声为主")
        with open(_dialog_path(movie_id), "w", encoding="utf-8") as f:
            f.write(text + "\n")
        meta = {
            "status": "done",
            "model": size,
            "language": info.language or language,
            "segments": kept,
            "chars": chars,
            "duration": round(total, 1),
            "elapsed": round(time.time() - started, 1),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error": "",
        }
        with open(_meta_path(movie_id), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        _set_job(movie_id, status="done", percent=100,
                 message="完成：%d 段 / %d 字" % (kept, chars), error="")
    except Exception as e:
        _set_job(movie_id, status="error", percent=0,
                 message="", error=str(e)[:400])
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def start(movie_id: int, video: str, max_seconds: int = 0,
          size: str = "", language: str = "",
          start_seconds: int = 0) -> Dict[str, Any]:
    """启动后台提取；重复调用返回正在进行的任务。"""
    if not lib_available():
        raise RuntimeError("未安装语音识别组件：pip install faster-whisper（或设置 AVM_ASR_LIBS）")
    cfg = load_config(refresh=True)
    a = (cfg.get("asr") or {})
    size = str(size or a.get("model") or "small")
    language = str(language or a.get("language") or "auto")
    if not model_available(size):
        raise RuntimeError("缺少语音模型：把 faster-whisper-%s 放到 %s（需要 model.bin）"
                           % (size, model_root(size)))
    with _JOBS_LOCK:
        cur = _JOBS.get(movie_id)
        if cur and cur.get("status") == "running":
            return dict(cur)
    _set_job(movie_id, status="running", percent=1, message="排队中…",
             error="", cancel=False)
    submit_task(_run, movie_id, video, cfg, size, language,
                int(max_seconds or 0), int(start_seconds or 0))
    return summary(movie_id)


def cancel(movie_id: int) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(movie_id)
        if job:
            job["cancel"] = True


def video_of(movie_id: int) -> Optional[str]:
    """取主视频路径（网络串流不做 ASR）。"""
    from . import store
    conn = connect()
    try:
        src = store.movie_play_source(conn, movie_id)
    finally:
        conn.close()
    if not src or src.get("strm_url"):
        return None
    return src.get("path")
