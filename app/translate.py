# -*- coding: utf-8 -*-
"""AI 字幕翻译：把一部片的字幕整轨译成目标语言，产出可直接播放的 SRT。

取材沿用 app/subtext.py 的优先级（内嵌中文文本轨 → 其他内嵌文本轨 → 外挂字幕），
但这里要的是**带时间码的条目**而不是纯文本，因此自己做 cue 级解析。

要点：
  * 按条打包送给模型，要求返回 JSON `[{"i":0,"t":"译文"}]`，索引对齐，不合并不拆分
  * 术语表（ai_glossary）强制照译，避免每部片女优名 / 系列名译法漂移
  * 打包失败自动降级为逐条翻译；单条仍失败则保留原文
  * 产物落 data/subs/{movie_id}/，并登记到 movie_subtitles，详情页可下载 / 删除
  * 支持双语（原文 + 译文）与纯译文
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import ai_prompts, subtext
from .ai_core import AIConfig, AiError, chat, register_prompt
from .config import DATA_DIR
from .db import connect


def _chat_json_list(cfg: Dict[str, Any], messages: List[Dict[str, str]],
                    max_tokens: int, purpose: str) -> List[Any]:
    """请求并解析 JSON **数组**（字幕翻译的返回是 [{"i":..,"t":..}]，不是对象）。"""
    text = chat(cfg, messages, max_tokens=max_tokens, purpose=purpose)
    start, end = text.find("["), text.rfind("]") + 1
    if start < 0 or end <= start:
        raise AiError("模型未返回 JSON 数组：%s" % text[:80])
    try:
        data = json.loads(text[start:end])
    except ValueError as e:
        raise AiError("模型返回的 JSON 数组无法解析：%s" % e)
    if not isinstance(data, list):
        raise AiError("模型返回的不是 JSON 数组")
    return data

register_prompt("subtitle.translate", "v1")

TARGETS = ("zh-Hans", "zh-Hant", "en")
MAX_CUES = 4000            # 单部片最多处理的字幕条数（防止异常字幕爆预算）
BATCH_CHARS = 1400         # 每个请求打包的原文长度

_SRT_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
_DIALOGUE_RE = re.compile(r"^dialogue:\s*[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,(.*)$",
                          re.IGNORECASE)
_TAG_RE = re.compile(r"\{[^}]*\}|<[^>]+>")


@dataclass
class Cue:
    start: float
    end: float
    text: str


def _to_sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def _fmt(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def parse_srt(raw: str) -> List[Cue]:
    cues: List[Cue] = []
    lines = (raw or "").splitlines()
    i = 0
    while i < len(lines):
        m = _SRT_TIME.search(lines[i])
        if not m:
            i += 1
            continue
        start = _to_sec(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _to_sec(m.group(5), m.group(6), m.group(7), m.group(8))
        i += 1
        body = []
        while i < len(lines) and lines[i].strip():
            body.append(lines[i].strip())
            i += 1
        text = _TAG_RE.sub("", " ".join(body)).strip()
        if text:
            cues.append(Cue(start, end, text))
    return cues


def parse_ass(raw: str) -> List[Cue]:
    cues: List[Cue] = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s.lower().startswith("dialogue:"):
            continue
        m = _DIALOGUE_RE.match(s)
        if not m:
            continue
        text = m.group(1).replace("\\N", " ").replace("\\n", " ")
        text = _TAG_RE.sub("", text).strip()
        if text:
            cues.append(Cue(0.0, 0.0, text))   # ASS 时间轴在 Format 里，朗读场景用不到
    return cues


def parse_any(raw: str, path: str) -> List[Cue]:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".ass", ".ssa"):
        return parse_ass(raw)
    return parse_srt(raw)


def srt_from_cues(cues: List[Cue], texts: List[str], bilingual: bool) -> str:
    out = []
    for n, (cue, text) in enumerate(zip(cues, texts), 1):
        body = cue.text + "\n" + text if bilingual else text
        out.append("%d\n%s --> %s\n%s\n" % (n, _fmt(cue.start), _fmt(cue.end), body))
    return "\n".join(out)


def list_glossary() -> List[Dict[str, str]]:
    with connect() as conn:
        rows = conn.execute("SELECT term, translation, kind FROM ai_glossary ORDER BY id").fetchall()
    return [{"term": r[0], "translation": r[1], "kind": r[2] or ""} for r in rows]


def replace_glossary(items: List[Dict[str, str]]) -> Dict[str, int]:
    rows = []
    for it in items:
        term = str(it.get("term") or "").strip()
        trans = str(it.get("translation") or "").strip()
        if term and trans:
            rows.append((term, trans, str(it.get("kind") or "term")))
    with connect() as conn:
        conn.execute("DELETE FROM ai_glossary")
        conn.executemany("INSERT INTO ai_glossary(term, translation, kind) VALUES(?,?,?)", rows)
    return {"count": len(rows)}


def _glossary_text() -> str:
    items = list_glossary()
    if not items:
        return ""
    return "\n".join("%s = %s" % (i["term"], i["translation"]) for i in items)


def collect_cues(files: List[Dict[str, Any]], sidecar: List[Dict[str, Any]],
                 cfg: Dict[str, Any]) -> Dict[str, Any]:
    """取材：返回带时间码的字幕条目（与 subtext.collect 同优先级）。"""
    from . import subprobe

    candidates: List[Tuple[Tuple[int, int], Dict[str, Any], Dict[str, Any]]] = []
    for f in files or []:
        path = f.get("path") or ""
        if not path or not os.path.isfile(path) or not f.get("sub_tracks"):
            continue
        try:
            info = json.loads(f["sub_tracks"])
        except (ValueError, TypeError):
            continue
        for t in (info.get("tracks") or []):
            if t.get("kind") != "text":
                continue
            bad = 1 if (t.get("forced") or t.get("partial")) else 0
            zh = 1 if t.get("zh") else 0
            candidates.append(((0 if zh else 1, bad), f, t))
    candidates.sort(key=lambda x: x[0])

    for _, f, t in candidates:
        raw = subprobe.extract_track_text(f["path"], int(t.get("si") or 0), cfg)
        cues = parse_srt(raw)
        if cues:
            label = "%s%s" % ((t.get("lang") or "").upper() or "UND",
                              (" · " + t["title"]) if t.get("title") else "")
            return {"cues": cues[:MAX_CUES], "source": "embedded", "track": label.strip(" ·")}

    for s in sorted([s for s in (sidecar or []) if s.get("path")],
                    key=lambda s: 0 if (s.get("lang") or "").lower().startswith("zh") else 1):
        path = s["path"]
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read(4_000_000)
        except OSError:
            continue
        cues = parse_any(raw, path)
        if cues:
            return {"cues": cues[:MAX_CUES], "source": "sidecar",
                    "track": os.path.basename(path)}
    return {"cues": [], "source": "", "track": ""}


def _batches(cues: List[Cue]) -> List[List[int]]:
    batches: List[List[int]] = []
    cur: List[int] = []
    n = 0
    for i, c in enumerate(cues):
        cur.append(i)
        n += len(c.text)
        if n >= BATCH_CHARS:
            batches.append(cur)
            cur, n = [], 0
    if cur:
        batches.append(cur)
    return batches


def translate_cues(cues: List[Cue], cfg: Dict[str, Any], target: str) -> Tuple[List[str], int]:
    """逐批翻译，返回（译文列表, 实际送出的原文字符数）。"""
    c = AIConfig.from_config(cfg)
    out: List[str] = [""] * len(cues)
    glossary = _glossary_text()
    sent = 0
    sys_msg = ai_prompts.render("subtitle.translate", cfg, target=target)

    for batch in _batches(cues):
        payload = [{"i": i, "t": cues[i].text} for i in batch]
        user = ("术语表（必须照译）：\n%s\n\n字幕 JSON：\n%s"
                % (glossary or "（无）", json.dumps(payload, ensure_ascii=False)))
        ok = False
        try:
            data = _chat_json_list(c, [{"role": "system", "content": sys_msg},
                                       {"role": "user", "content": user}],
                                   max_tokens=600 * max(1, len(batch) // 8 + 1),
                                   purpose="subtitle.translate")
            for item in data:
                if not isinstance(item, dict):
                    continue
                idx = item.get("i")
                txt = str(item.get("t") or "").strip()
                if isinstance(idx, int) and 0 <= idx < len(cues) and txt:
                    out[idx] = txt
                    ok = True
            sent += sum(len(cues[i].text) for i in batch)
        except AiError:
            ok = False
        if not ok:
            # 整批失败：降级为逐条，单条再失败就保留原文
            for i in batch:
                sent += len(cues[i].text)
                try:
                    data = _chat_json_list(
                        c, [{"role": "system", "content": sys_msg},
                            {"role": "user", "content": json.dumps(
                                [{"i": 0, "t": cues[i].text}], ensure_ascii=False)}],
                        max_tokens=200, purpose="subtitle.translate.one")
                    out[i] = (str(data[0].get("t") or "").strip() or cues[i].text
                              if data and isinstance(data[0], dict) else cues[i].text)
                except AiError:
                    out[i] = cues[i].text

    return out, sent


def _sub_dir(movie_id: int) -> str:
    d = os.path.join(str(DATA_DIR), "subs", str(movie_id))
    os.makedirs(d, exist_ok=True)
    return d


def register(conn: sqlite3.Connection, movie_id: int, path: str, lang: str) -> int:
    cur = conn.execute(
        "INSERT INTO movie_subtitles(movie_id, file_path, filename, lang, source) "
        "VALUES(?,?,?,?,'ai-translate')",
        (movie_id, path, os.path.basename(path), lang))
    return int(cur.lastrowid)


def unregister(conn: sqlite3.Connection, movie_id: int, sub_id: int) -> Optional[str]:
    row = conn.execute(
        "SELECT id, file_path, source FROM movie_subtitles WHERE id=? AND movie_id=?",
        (int(sub_id), int(movie_id))).fetchone()
    if not row or row["source"] != "ai-translate":
        return None
    conn.execute("DELETE FROM movie_subtitles WHERE id=?", (row["id"],))
    return row["file_path"]


def translate_movie(movie_id: int, cfg: Dict[str, Any], target: str = "zh-Hans",
                    bilingual: bool = False) -> Dict[str, Any]:
    """为单部片生成翻译字幕；无可用字幕时抛 ValueError（接口层转 400）。"""
    if target not in TARGETS:
        raise ValueError("不支持的目标语言：%s" % target)
    with connect() as conn:
        m = _movie_files_and_subs(conn, movie_id)
        if not m:
            raise ValueError("影片不存在或未入库")
        got = collect_cues(m["files"], m["subtitles"], cfg)
        if not got["cues"]:
            raise ValueError("没有可用字幕：请先让扫描识别内嵌字幕，或放置 .srt/.ass 外挂字幕")
        texts, sent = translate_cues(got["cues"], cfg, target)
        if not any(texts):
            raise ValueError("翻译结果为空，请检查模型配置或稍后重试")
        # 没译到的条目回填原文，保证时间码与条数不错位
        texts = [t or got["cues"][i].text for i, t in enumerate(texts)]
        name = "%s%s.srt" % (target, "-双语" if bilingual else "")
        path = os.path.join(_sub_dir(movie_id), name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(srt_from_cues(got["cues"], texts, bilingual))
        sub_id = register(conn, movie_id, path, target)
        conn.commit()
    return {
        "ok": True, "subtitle_id": sub_id, "path": path,
        "cues": len(got["cues"]), "chars": sent, "target": target,
        "bilingual": bilingual, "source": got["source"], "track": got["track"],
    }


def _movie_files_and_subs(conn: sqlite3.Connection, movie_id: int) -> Optional[Dict[str, Any]]:
    from . import store
    mv = store.movie_detail(conn, movie_id)
    if not mv:
        return None
    return {"files": mv.get("files") or [], "subtitles": mv.get("subtitles") or []}
