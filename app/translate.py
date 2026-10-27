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
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import ai_core, ai_prompts, subtext
from .ai_core import AIConfig, AiError, chat, log, register_prompt
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
BATCH_CHARS = 4000         # 每个请求打包的原文长度（大多数影片一次即可译完，连贯性更好）
MAX_GLOSSARY = 200         # 单批最多注入的术语条数：术语表再大也不至于把 prompt 撑爆

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


def list_glossary(target: str = "") -> List[Dict[str, str]]:
    """术语表。target 非空时只返回「通用」+「该目标语言」的条目。

    lang 为空或 'any' 表示通用；指定语言（如 zh-Hant）只在该目标下注入，
    避免翻英文时把中文译名当噪声塞给模型。
    """
    sql = "SELECT term, translation, kind, COALESCE(lang, '') AS lang FROM ai_glossary"
    args: Tuple[Any, ...] = ()
    if target:
        sql += " WHERE COALESCE(lang, '') IN ('', 'any', ?)"
        args = (target,)
    sql += " ORDER BY id"
    conn = connect()
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [{"term": r[0], "translation": r[1], "kind": r[2] or "", "lang": r[3] or ""}
            for r in rows]


def replace_glossary(items: List[Dict[str, str]]) -> Dict[str, int]:
    rows = []
    langs = set()
    for it in items:
        term = str(it.get("term") or "").strip()
        trans = str(it.get("translation") or "").strip()
        if term and trans:
            lang = str(it.get("lang") or "").strip()
            rows.append((term, trans, str(it.get("kind") or "term"), lang))
            langs.add(lang or "any")
    conn = connect()
    try:
        conn.execute("DELETE FROM ai_glossary")
        conn.executemany(
            "INSERT INTO ai_glossary(term, translation, kind, lang) VALUES(?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    return {"count": len(rows), "langs": sorted(langs)}


def glossary_hits(text: str, target: str = "") -> List[Dict[str, str]]:
    """挑出「本段字幕里真的出现过」的术语（按 term 做子串匹配）。

    只注入命中项：术语表几百条时，全量塞给每一批会让 prompt 线性膨胀，
    而且大量无关条目会稀释「必须照译」这条指令。
    """
    src = str(text or "")
    if not src:
        return []
    out = []
    for item in list_glossary(target):
        term = item["term"]
        if term and term in src:
            out.append(item)
            if len(out) >= MAX_GLOSSARY:
                break
    return out


def _glossary_block(hits: List[Dict[str, str]]) -> str:
    if not hits:
        return ""
    return "术语表（必须照译）：\n%s\n\n" % "\n".join(
        "%s = %s" % (h["term"], h["translation"]) for h in hits)


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



def _truncated(err: BaseException) -> bool:
    """是不是「输出被 max_tokens 截断」——这类错误拆小批次就能成功，值得重试。"""
    return "token 上限截断" in str(err) or "finish_reason=length" in str(err)


def translate_cues(cues: List[Cue], cfg: Dict[str, Any], target: str,
                   progress: Optional[Callable[[int, int], None]] = None) -> Tuple[List[str], int]:
    """逐批翻译，返回（译文列表, 实际送出的原文字符数）。

    progress(done, total)：每完成一个顶层批次回调一次；回调**抛异常即中止翻译**
    （后台任务用它响应「取消」）。
    """
    c = AIConfig.from_config(cfg)
    out: List[str] = [""] * len(cues)
    sent = 0
    sys_msg = ai_prompts.render("subtitle.translate", cfg, target=target)

    carry = ""   # 上一批末尾几条（原文+译文），作为语气与术语的衔接参考

    all_batches = _batches(cues)
    total_batches = max(1, len(all_batches))
    done_batches = 0

    def _tick() -> None:
        nonlocal done_batches
        done_batches += 1
        if progress is not None:
            progress(done_batches, total_batches)

    def run_batches(batches, depth=0):
        nonlocal carry
        for batch in batches:
            _one(batch, depth)
            _tick()

    def _one(batch, depth=0):
        nonlocal carry, sent
        if not batch:
            return
        payload = [{"i": i, "t": cues[i].text} for i in batch]
        # 只带本批字幕里真出现过的术语：术语表再大，prompt 也不会随批次数膨胀
        block = _glossary_block(glossary_hits("\n".join(cues[i].text for i in batch), target))
        user = block + carry + "字幕 JSON：\n" + json.dumps(payload, ensure_ascii=False)
        ok = False
        err_msg = ""
        try:
            data = _chat_json_list(c, [{"role": "system", "content": sys_msg},
                                       {"role": "user", "content": user}],
                                   max_tokens=min(6000, 240 + len(batch) * 40),
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
            # 记住本批末尾两行，下一批 prompt 里带上——跨批语气与指代才连得上
            tail = [(cues[i].text, out[i]) for i in batch[-2:] if out[i]]
            if tail:
                carry = ("上文参考（保持称呼、语气与译名一致，**不要重复翻译**）：\n"
                         + "\n".join("%s => %s" % (a[:80], b[:80]) for a, b in tail) + "\n\n")
        except AiError as e:
            ok = False
            err_msg = str(e)
        if not ok and depth < 2 and len(batch) > 1 and _truncated(AiError(err_msg)):
            # 多半是输出被 max_tokens 截断（推理型模型常见）：对半拆开重试
            mid = len(batch) // 2
            log.info("批次 %d 条失败或截断，拆成 %d + %d 重试",
                     len(batch), mid, len(batch) - mid)
            _one(batch[:mid], depth + 1)
            _one(batch[mid:], depth + 1)
            return
        if not ok:
            # 整批失败：降级为逐条，单条再失败就保留原文。
            # 降级同样带术语表——模型不听话时更需要译名约束。
            for i in batch:
                sent += len(cues[i].text)
                try:
                    data = _chat_json_list(
                        c, [{"role": "system", "content": sys_msg},
                            {"role": "user", "content": block + json.dumps(
                                [{"i": 0, "t": cues[i].text}], ensure_ascii=False)}],
                        max_tokens=200, purpose="subtitle.translate.one")
                    out[i] = (str(data[0].get("t") or "").strip() or cues[i].text
                              if data and isinstance(data[0], dict) else cues[i].text)
                except AiError:
                    out[i] = cues[i].text

    run_batches(all_batches)
    return out, sent


def _sub_dir(movie_id: int) -> str:
    d = os.path.join(str(DATA_DIR), "subs", str(movie_id))
    os.makedirs(d, exist_ok=True)
    return d


def register(conn: sqlite3.Connection, movie_id: int, path: str, lang: str) -> int:
    """登记 AI 译文：同影片同语言**只保留一条**——重新翻译覆盖文件并更新记录。

    以前每次翻译都 INSERT 一条新记录，而产物文件名固定（<lang>.srt）会被覆盖，
    结果是多条记录指向同一个文件；删除其中一条会把文件删掉，其余记录全部失效
    （播放器菜单里字幕「凭空消失」）。"""
    row = conn.execute(
        "SELECT id FROM movie_subtitles WHERE movie_id=? AND lang=? AND source='ai-translate'",
        (movie_id, lang)).fetchone()
    if row:
        conn.execute("UPDATE movie_subtitles SET file_path=?, filename=? WHERE id=?",
                     (path, os.path.basename(path), row["id"]))
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO movie_subtitles(movie_id, file_path, filename, lang, source) "
        "VALUES(?,?,?,?,'ai-translate')",
        (movie_id, path, os.path.basename(path), lang))
    return int(cur.lastrowid)


def unregister(conn: sqlite3.Connection, movie_id: int, sub_id: int) -> Optional[str]:
    """删除字幕登记，返回文件路径（由调用方决定是否删盘上的文件）。

    以前只允许删 ai-translate，导致 ASR 草稿字幕删除时返回 404、记录删不掉。
    现在任何来源都能注销：data/subs 下的产物会被一并删除，视频目录里的外挂文件保留。
    """
    row = conn.execute(
        "SELECT id, file_path, source FROM movie_subtitles WHERE id=? AND movie_id=?",
        (int(sub_id), int(movie_id))).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM movie_subtitles WHERE id=?", (row["id"],))
    return row["file_path"]


def translate_movie(movie_id: int, cfg: Dict[str, Any], target: str = "zh-Hans",
                    bilingual: bool = False,
                    progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
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
        # 用「实际送出的 prompt 字符数」计费：包含 system 提示词与术语表，
        # 这样任务明细里的 chars、批次预算和真实消耗是同一个口径。
        # （并发跑多部时会把别人的用量一并算进来，属于偏保守的高估。）
        before = int(ai_core.stats().get("prompt_chars", 0))
        texts, sent = translate_cues(got["cues"], cfg, target, progress=progress)
        used = max(0, int(ai_core.stats().get("prompt_chars", 0)) - before)
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
        "cues": len(got["cues"]), "chars": used or sent, "cue_chars": sent,
        "target": target, "bilingual": bilingual,
        "source": got["source"], "track": got["track"],
    }


def _movie_files_and_subs(conn: sqlite3.Connection, movie_id: int) -> Optional[Dict[str, Any]]:
    from . import store
    mv = store.movie_detail(conn, movie_id)
    if not mv:
        return None
    return {"files": mv.get("files") or [], "subtitles": mv.get("subtitles") or []}
