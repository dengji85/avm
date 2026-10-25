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

import glob
import importlib.util
import json
import os
import re
import shutil
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
ASR_SEG_SECONDS = 600      # 转写切片：10 分钟一块，控制单次 STFT 内存

_JOBS: Dict[int, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_MODEL_CACHE: Dict[Tuple[str, str, str], Any] = {}
_MODEL_LOCK = threading.Lock()

_CLOCK = re.compile(r"^\s*\[(\d+):(\d{2})\]")
# dialog.txt 的一整行（含台词正文），旧数据兜底用
CLOCK_LINE = re.compile(r"^\s*\[(\d+):(\d{2})\]\s*(.+)$")


def beam_size(cfg: Dict[str, Any] | None) -> int:
    """识别速度/质量档。

    ``fast`` 用 beam=1：这类片子对白清晰、基本单人说话，束宽几乎不影响准确度，
    但速度快 2~3 倍；``accurate``（默认）维持 beam=5。
    """
    q = str(((cfg or {}).get("asr") or {}).get("quality") or "accurate").strip().lower()
    return 1 if q == "fast" else 5


def _asr_cfg(refresh: bool = False) -> Dict[str, Any]:
    return (load_config(refresh=refresh) or {}).get("asr") or {}


def libs_dir(refresh: bool = False) -> str:
    """依赖目录：环境变量 > 设置页 asr.libs_dir > 组件页安装位（data/ext/asr-libs）。"""
    env = os.environ.get("AVM_ASR_LIBS", "").strip()
    if env:
        return env
    custom = str(_asr_cfg(refresh).get("libs_dir") or "").strip()
    if custom:
        return custom
    try:
        from .ext_components import asr_libs_dir as _ext_libs
        return _ext_libs()
    except Exception:
        return ""


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
        "tmp_dir": str(a.get("tmp_dir") or os.path.join(str(DATA_DIR), "asr")),
        "language": str(a.get("language") or "ja"),
    }


def _dialog_dir(movie_id: int) -> str:
    d = os.path.join(str(DATA_DIR), "asr", str(movie_id))
    os.makedirs(d, exist_ok=True)
    return d


def tmp_dir(movie_id: int) -> str:
    """临时音轨目录：可配置到其它盘，避免提取时把 C 盘撑满。

    对白文本/时间轴仍留在 data/asr（很小），只有那个 32KB/秒的临时 wav 会挪走。
    """
    custom = str(_asr_cfg().get("tmp_dir") or "").strip()
    d = os.path.join(custom, str(movie_id)) if custom else _dialog_dir(movie_id)
    os.makedirs(d, exist_ok=True)
    return d


def _need_mb(video: str, ffmpeg: str, max_seconds: int) -> int:
    """估算临时 wav 需要多少 MB（探测失败时给一个保守下限）。"""
    dur = 0.0
    try:
        ffprobe = "ffprobe"
        if ffmpeg and os.path.isabs(ffmpeg):
            cand = os.path.join(os.path.dirname(ffmpeg),
                                "ffprobe.exe" if os.name == "nt" else "ffprobe")
            if os.path.isfile(cand):
                ffprobe = cand
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", video],
            capture_output=True, text=True, timeout=30)
        dur = float(out.stdout.strip() or 0)
    except Exception:
        dur = 0.0
    if max_seconds and max_seconds > 0:
        dur = min(dur, float(max_seconds)) if dur > 0 else float(max_seconds)
    if dur <= 0:
        return 600
    return int(dur * 16000 * 2 / 1048576 * 1.15) + 20


def _dialog_path(movie_id: int) -> str:
    return os.path.join(_dialog_dir(movie_id), "dialog.txt")


def _meta_path(movie_id: int) -> str:
    return os.path.join(_dialog_dir(movie_id), "meta.json")


def _segments_path(movie_id: int) -> str:
    return os.path.join(_dialog_dir(movie_id), "segments.json")


def read_segments(movie_id: int) -> List[Dict[str, Any]]:
    """逐段时间轴（start/end/text）；旧版本只存了 dialog.txt，这里会返回空。"""
    try:
        with open(_segments_path(movie_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _srt_time(sec: float) -> str:
    ms = max(0, int(round(float(sec or 0) * 1000)))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _segments_from_clock_text(movie_id: int) -> List[Dict[str, Any]]:
    """旧数据兜底：dialog.txt 只有 [分:秒] 起始时间，用下一条起点推算结束时间。

    对白之间的间隔通常就是上一条的显示时长；遇到长时间没对白的长片，
    结束时间封顶 8 秒，避免一条字幕挂在屏幕上十几分钟。
    """
    try:
        with open(_dialog_path(movie_id), "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    segs: List[Dict[str, Any]] = []
    for line in lines:
        m = CLOCK_LINE.match(line)
        if not m:
            continue
        start = int(m.group(1)) * 60 + int(m.group(2))
        segs.append({"start": float(start), "end": 0.0, "text": m.group(3).strip()})
    for i, s in enumerate(segs):
        nxt = segs[i + 1]["start"] if i + 1 < len(segs) else s["start"] + 5
        s["end"] = min(max(s["start"] + 1.2, nxt), s["start"] + 8)
    return segs


def export_srt(movie_id: int, lang: str = "ja") -> Dict[str, Any]:
    """把对白导出成草稿 SRT 并登记为字幕（可反复导出，文件覆盖、记录复用）。

    新数据用精确的 start/end；旧数据（只有 [分:秒] 起始时间）用推算时间轴，
    并在返回值里标记 estimated=True，省掉重跑整片识别的时间。
    """
    segs = read_segments(movie_id)
    estimated = False
    if not segs:
        segs = _segments_from_clock_text(movie_id)
        estimated = True
    if not segs:
        raise RuntimeError("这部片还没有提取过对白，请先提取对白")
    subs_dir = os.path.join(str(DATA_DIR), "subs", str(movie_id))
    os.makedirs(subs_dir, exist_ok=True)
    path = os.path.join(subs_dir, "asr-%s.srt" % (lang or "ja"))
    blocks: List[str] = []
    for s in segs:
        text = str(s.get("text") or "").strip()
        if not text:
            continue
        start = float(s.get("start") or 0)
        end = max(float(s.get("end") or 0), start + 0.6)
        blocks.append("%d\n%s --> %s\n%s\n"
                      % (len(blocks) + 1, _srt_time(start), _srt_time(end), text))
    if not blocks:
        raise RuntimeError("没有可导出的对白")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(blocks))
    from . import subtitles as subs_mod
    conn = connect()
    try:
        added = subs_mod._register_subtitle(conn, movie_id, path, "asr")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "path": path, "cues": len(blocks),
            "added": bool(added), "estimated": estimated}


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
        # 有没有精确时间轴（决定能不能导出草稿字幕）
        "has_segments": bool(read_segments(movie_id)),
    }
    if preview > 0:
        text = dialog_text(movie_id)
        out["text"] = text[:preview]
        out["has_text"] = bool(text)
    return out


def clear(movie_id: int) -> None:
    with _JOBS_LOCK:
        _JOBS.pop(movie_id, None)
    for p in (_dialog_path(movie_id), _meta_path(movie_id), _segments_path(movie_id)):
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


def _split_wav(wav: str, ffmpeg: str, out_dir: str,
               seg_seconds: int = ASR_SEG_SECONDS) -> List[str]:
    """把整条 wav 按固定时长切块；失败时退回整条。"""
    if seg_seconds <= 0:
        return [wav]
    for old in glob.glob(os.path.join(out_dir, "part_*.wav")):
        try:
            os.remove(old)
        except OSError:
            pass
    pattern = os.path.join(out_dir, "part_%03d.wav")
    proc = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", wav, "-f", "segment",
         "-segment_time", str(int(seg_seconds)), "-c", "copy", pattern],
        capture_output=True)
    if proc.returncode != 0:
        return [wav]
    parts = sorted(glob.glob(os.path.join(out_dir, "part_*.wav")))
    return parts or [wav]


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
    wav = os.path.join(tmp_dir(movie_id), "_audio.wav")
    try:
        ffmpeg = str(cfg.get("ffmpeg_path") or "ffmpeg").strip() or "ffmpeg"
        need = _need_mb(video, ffmpeg, max_seconds)
        free = shutil.disk_usage(tmp_dir(movie_id)).free / 1048576.0
        if free < need:
            raise RuntimeError(
                "临时目录空间不足：需要约 %d MB，当前只剩 %d MB。"
                "可在「设置 → AI 增强 → 语音对白提取」里把临时目录改到其它盘。"
                % (need, int(free)))
        _set_job(movie_id, status="running", percent=2, message="抽取音轨…", error="")
        duration = _prepare_audio(video, wav, ffmpeg, max_seconds, start_seconds)
        if _job_cancelled(movie_id):
            raise RuntimeError("已取消")

        _set_job(movie_id, percent=8, message="加载语音模型…")
        model = _load_model(size, "cpu")
        if _job_cancelled(movie_id):
            raise RuntimeError("已取消")

        _set_job(movie_id, percent=12, message="识别对白…")
        # 分段转写：整段音频直接交给 feature_extractor 会一次性做 STFT，
        # 2 小时以上的片要 2.5GB+ 内存（曾直接 MemoryError）。按 10 分钟切片后
        # 每片只要 100MB 左右，长片也能稳定跑完。
        parts = _split_wav(wav, ffmpeg, tmp_dir(movie_id))
        sys.stderr.write("[asr] movie %s: audio %.0fs -> %d part(s)\n"
                         % (movie_id, duration, len(parts)))
        total = float(duration or 0)
        lines: List[str] = []
        # 同时留一份带 start/end 的逐段数据：后面导出草稿字幕全靠它
        segs: List[Dict[str, Any]] = []
        last = ""
        kept = chars = 0
        offset = 0.0
        for part in parts:
            if _job_cancelled(movie_id):
                raise RuntimeError("已取消")
            # 注意：不用内置 vad_filter——它依赖 onnxruntime，在部分环境会直接崩溃；
            # 噪声交给 _keep() 在文本层面过滤，效果等价且稳定。
            segments, info = model.transcribe(
                part,
                language=None if language in ("", "auto") else language,
                beam_size=beam_size(cfg),
                vad_filter=False,
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
                log_prob_threshold=LOGPROB_MIN,
                compression_ratio_threshold=COMPRESS_MAX,
                chunk_length=30,
            )
            part_dur = float(info.duration or 0)
            for seg in segments:
                if _job_cancelled(movie_id):
                    raise RuntimeError("已取消")
                text = (seg.text or "").strip()
                start = offset + float(seg.start or 0)
                end = offset + float(seg.end or 0)
                if _keep(seg, text, last, language):
                    lines.append("[%s] %s" % (_clock(start), text))
                    segs.append({
                        "start": round(start, 2),
                        "end": round(end, 2),
                        "text": text,
                        "logprob": round(float(getattr(seg, "avg_logprob", 0) or 0), 3),
                    })
                    last = text
                    kept += 1
                    chars += len(text)
                if total > 0:
                    pct = 12 + int(83 * min(1.0, end / total))
                    _set_job(movie_id, percent=pct, message="已识别 %d 段" % kept)
            offset += part_dur

        text = "\n".join(lines)
        if not text:
            raise RuntimeError("没有识别到可用对白：这段音频可能以环境音/拟声为主")
        with open(_dialog_path(movie_id), "w", encoding="utf-8") as f:
            f.write(text + "\n")
        with open(_segments_path(movie_id), "w", encoding="utf-8") as f:
            json.dump(segs, f, ensure_ascii=False)
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
        try:
            import traceback
            sys.stderr.write("[asr] extract failed (movie %s): %s\n%s\n"
                             % (movie_id, e, traceback.format_exc()))
        except Exception:
            pass
        _set_job(movie_id, status="error", percent=0,
                 message="", error=str(e)[:400])
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
        for leftover in glob.glob(os.path.join(tmp_dir(movie_id), "part_*.wav")):
            try:
                os.remove(leftover)
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
