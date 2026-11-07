# -*- coding: utf-8 -*-
"""在线字幕搜索编排：并行、限时、缓存、去重择优、下载。

设计要点（都是实测出来的坑）：
1. 只把「番号字符串」发出去，绝不上传文件名、路径、视频或音频。
2. 带数字前缀的冷门番号（812MMC-010）各站写法不统一，一次生成多个变体；
   先并行查所有源的主写法，没找够才并行查备用写法，避免无谓的请求。
3. 源数量再多，单次搜索的耗时上限仍是 search_timeout：所有源并行、
   按 as_completed 收结果，到点就返回已拿到的候选，慢源不拖住页面。
4. 不限语言：任何语种都保留（可交给 AI 翻译），语言只影响排序不影响去留。
5. 候选的完整下载地址只留服务端；下载前统一校验域名白名单，避免 SSRF。

站点解析在 app/subtitle_sources/ 下，各源失败一律降级，不影响其它源。
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from . import subqc
from . import subtitle_sources as sources
from .subtitle_sources import common

# 兼容旧引用（展示名 / 下载白名单 / 大小上限）
PROVIDER_LABELS = sources.LABELS
ALLOWED_HOSTS = sources.ALLOWED_HOSTS
MAX_BYTES = common.MAX_BYTES

_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


def _sub_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    s = dict(cfg.get("subtitles") or {})
    defaults = {
        "enabled": True,
        # 默认全开；需要凭据的源没配 Key 会明确显示「未启用」，不假装查过
        "providers": ["subtitlecat", "thunder", "opensubtitles", "assrt", "custom"],
        "timeout": 15,               # 单次 HTTP 超时
        "delay_ms": 800,             # 每个源的请求间隔
        "max_candidates": 24,
        "proxy": "",
        "user_agent": common.DEFAULT_UA,
        # 并行与限时：源再多也只等这么久
        "search_timeout": 12,
        "max_workers": 10,
        # 番号写法：主写法查不到才试备用写法
        "max_variants": 4,
        "variant_rounds": 2,
        "enough_candidates": 6,      # 已有这么多候选就不再试备用写法
        "cache_ttl": 300,            # 搜索结果短缓存，重复点搜索秒回
        # 单源抓取上限
        "max_per_source": 20,
        "per_page": 6,
        "max_pages": 4,
        # 凭据（GET /config 不下发，PUT /config 空串 = 不修改）
        "opensubtitles_key": "",
        "assrt_token": "",
        "custom_sources": [],
        "lang_priority": ["简体中文", "日中双语", "繁体中文", "中文", "日文", "英文"],
    }
    for k, v in defaults.items():
        s.setdefault(k, v)
    # 字幕源没有单独配代理时沿用刮削代理，避免用户配了两遍
    if not s["proxy"]:
        s["proxy"] = (cfg.get("scraper") or {}).get("proxy") or ""
    return s


def _cache_key(parts: Any) -> str:
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _cache_get(key: str, ttl: int) -> Dict[str, Any] | None:
    if ttl <= 0:
        return None
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if not item:
            return None
        if time.time() - item[0] > ttl:
            _CACHE.pop(key, None)
            return None
        return item[1]
    return None


def _cache_put(key: str, result: Dict[str, Any], ttl: int) -> None:
    if ttl <= 0:
        return
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), result)
        if len(_CACHE) > 256:        # 只留最近 128 部影片的结果
            for k in sorted(_CACHE, key=lambda x: _CACHE[x][0])[:len(_CACHE) - 128]:
                _CACHE.pop(k, None)


def _safe_search(name: str, variant: str, scfg: Dict[str, Any],
                 ctx: Dict[str, Any]) -> Dict[str, Any]:
    mod = sources.module_of(name)
    if mod is None:
        return common.off("字幕源未注册：%s" % name)
    try:
        r = mod.search(variant, scfg, ctx)
        return r if isinstance(r, dict) else common.degraded("parse", "字幕源返回格式异常")
    except Exception as exc:         # 单源炸了不能影响其它源；绝不带出 URL / 凭据
        return common.degraded("error", "%s 查询失败：%s"
                               % (sources.label_for_status(name, scfg), type(exc).__name__))


def _run_batch(batch: List[Tuple[str, int, str]], scfg: Dict[str, Any],
               variants: List[str], code: str, deadline: float,
               results: Dict[str, Dict[int, Dict[str, Any]]]) -> None:
    """把一批 (源, 变体序号, 番号写法) 全部提交，按完成顺序收，到点即走。"""
    if not batch:
        return
    left = max(0.1, deadline - time.monotonic())
    if left <= 0:
        return
    lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=min(int(scfg["max_workers"]), len(batch)),
                              thread_name_prefix="subsearch")
    pending = {}
    # 备用写法是并发重试，同源间隔缩短，否则 800ms 限流会把并行收益吃光
    retry_cfg = dict(scfg, delay_ms=min(int(scfg["delay_ms"] or 0), 250))
    try:
        for name, idx, variant in batch:
            ctx = {"code": code, "variants": variants, "variant_index": idx}
            if name.startswith("custom:"):
                ctx["custom_index"] = name.split(":", 1)[1]
            use_cfg = retry_cfg if idx else scfg
            fut = pool.submit(_safe_search, name, variant, use_cfg, ctx)
            pending[fut] = (name, idx)
        try:
            for fut in as_completed(pending, timeout=left):
                name, idx = pending[fut]
                r = fut.result()
                with lock:
                    results.setdefault(name, {})[idx] = r
        except TimeoutError:
            pass                     # 到点就返回已拿到的结果
    finally:
        # 卡住的源自行收尾，绝不阻塞请求线程
        pool.shutdown(wait=False, cancel_futures=True)


def _merge_source(results: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """一个源查了多个番号写法，合并成一条源状态 + 去重后的候选。"""
    ordered = [results[i] for i in sorted(results)]
    cands: List[Dict[str, Any]] = []
    seen = set()
    for r in ordered:
        for c in (r.get("candidates") or []):
            u = c.get("url") or ""
            if u and u not in seen:
                seen.add(u)
                cands.append(c)
    if cands:
        return common.ok(cands)
    for r in ordered:
        if r.get("status") == "off":
            return r
    for r in ordered:
        if r.get("status") in ("degraded", "error"):
            return r
    return ordered[-1] if ordered else common.degraded("timeout", "查询超时")


def _hint(cands: List[Dict[str, Any]], src: List[Dict[str, Any]],
          native_count: int) -> str:
    if cands:
        return "" if native_count else "all_foreign"
    if any(s["status"] == "off" for s in src):
        return "need_credentials"
    if not src:
        return "no_source"
    return "no_result"


def search_all(code: str, cfg: Dict[str, Any], video_seconds: float = 0) -> Dict[str, Any]:
    """并行查所有启用的源，合并候选、跨源择优并排序。

    返回结构与前端约定不变：{code, candidates, sources, video_seconds}，
    另加 best_id / elapsed_ms / cached / hint 供择优与提示。
    """
    started = time.monotonic()
    scfg = _sub_cfg(cfg)
    base = {"code": code, "candidates": [], "sources": [],
            "video_seconds": video_seconds, "best_id": "", "elapsed_ms": 0,
            "cached": False, "hint": ""}
    if not scfg.get("enabled", True):
        return dict(base, disabled=True)
    code = (code or "").strip()
    if not code:
        return base

    # 校验用全部写法（站点可能写成 MMC-10），出站只发前 N 个写法，
    # 既提高命中率又不把请求数放大。
    all_variants = common.code_variants(code, 8)
    variants = all_variants[:max(1, int(scfg["max_variants"] or 4))]
    names = sources.enabled_names(scfg)
    key = _cache_key([code, all_variants, variants, names, int(video_seconds) // 60,
                      bool(scfg["opensubtitles_key"]), bool(scfg["assrt_token"]),
                      scfg["custom_sources"], scfg["lang_priority"]])
    hit = _cache_get(key, int(scfg["cache_ttl"] or 0))
    if hit:
        return dict(hit, cached=True, elapsed_ms=0)

    active: List[str] = []
    skipped: Dict[str, str] = {}
    for name in names:
        err = sources.credential_error(name, scfg)
        if err:
            skipped[name] = err
        else:
            active.append(name)

    budget = max(2.0, float(scfg["search_timeout"] or 12))
    deadline = started + budget
    # 单源 HTTP 超时必须小于总预算的一半：否则一个慢源就能吃光第一轮，
    # 备用番号写法根本没机会查，源越多反而越容易整轮超时。
    scfg["timeout"] = max(3, min(int(scfg["timeout"] or 15), int(budget) // 2))
    results: Dict[str, Dict[int, Dict[str, Any]]] = {}

    # 第一轮：所有源并行查主写法（原样番号）。源数增加不会拉长这一轮。
    _run_batch([(n, 0, variants[0]) for n in active], scfg, all_variants, code,
               deadline, results)
    first = [c for r in results.values() for x in r.values()
             for c in (x.get("candidates") or [])]
    best_first = 0
    if first:
        best_first = max(subqc.estimate_score(c, video_seconds, scfg["lang_priority"])
                         for c in first)

    # 第二轮：没找够、且预算还有余量时，只让「没命中」的源试备用写法
    rounds = max(0, min(int(scfg["variant_rounds"] or 0), len(variants) - 1))
    if rounds and len(first) < int(scfg["enough_candidates"] or 6) \
            and best_first < 85 and time.monotonic() < deadline:
        batch = []
        for name in active:
            rs = results.get(name, {})
            if 0 not in rs:          # 第一轮没回来（超时）就别再给它加请求
                continue
            if _merge_source(rs).get("candidates"):
                continue
            for idx in range(1, rounds + 1):
                batch.append((name, idx, variants[idx]))
        _run_batch(batch, scfg, all_variants, code, deadline, results)

    # 汇总：一个源一条状态，任何单源失败都不影响其它源
    src: List[Dict[str, Any]] = []
    cands: List[Dict[str, Any]] = []
    for name in names:
        if name in skipped:
            src.append({"provider": name, "label": sources.label_for_status(name, scfg),
                        "status": "off", "reason": "not_configured",
                        "message": skipped[name], "count": 0, "tried": 0})
            continue
        rs = results.get(name, {})
        merged = _merge_source(rs) if rs else common.degraded("timeout", "查询超时")
        got = merged.get("candidates") or []
        cands.extend(got)
        src.append({
            "provider": name, "label": sources.label_for_status(name, scfg),
            "status": merged.get("status", "degraded"),
            "reason": merged.get("reason", ""),
            "message": merged.get("message", ""),
            "count": len(got), "tried": len(rs),
        })

    # 下载前估分 → 跨源同内容合并（保留分最高的那条）→ 排序
    cands = subqc.dedupe(cands, limit=max(60, int(scfg["max_candidates"] or 24) * 4))
    for c in cands:
        c["estimate"] = subqc.estimate_score(c, video_seconds, scfg["lang_priority"])
        c["id"] = hashlib.sha1((str(c.get("provider") or "") + "|" +
                                str(c.get("url") or "")).encode("utf-8")).hexdigest()[:12]
        c["provider_label"] = sources.label_of(str(c.get("provider") or ""), c)
        c["can_translate"] = c.get("lang") not in ("简体中文", "繁体中文", "日中双语", "中文")
    cands = subqc.merge_similar(cands)
    cands.sort(key=lambda c: (-c["estimate"], -int(c.get("downloads") or 0),
                              c.get("provider") or "", c.get("name") or ""))
    limit = max(1, int(scfg["max_candidates"] or 24))
    cands = cands[:limit]

    native_count = sum(1 for c in cands if not c["can_translate"])
    result = {
        "code": code, "candidates": cands, "sources": src,
        "video_seconds": video_seconds,
        "best_id": cands[0]["id"] if cands else "",
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "cached": False,
        "hint": _hint(cands, src, native_count),
    }
    _cache_put(key, result, int(scfg["cache_ttl"] or 0))
    return result


def guard_url(url: str, cand: Dict[str, Any]) -> str:
    """下载前统一校验域名；自定义源只能用自己显式声明的域名。"""
    host = (urlparse(url or "").hostname or "").lower()
    is_custom = str(cand.get("provider") or "").startswith("custom")
    extra = {str(h).lower() for h in (cand.get("allowed_hosts") or [])}
    allowed = extra if is_custom else set(sources.ALLOWED_HOSTS) | extra
    suffixes = () if is_custom else sources.ALLOWED_SUFFIXES
    if host in allowed or (suffixes and any(host.endswith(s) for s in suffixes)):
        return url
    raise ValueError("字幕地址不在允许的来源域名内")


def _maybe_decompress(raw: bytes) -> bytes:
    """部分源返回 gzip / zip 字幕，取出里面的文本文件再交给归一落盘。"""
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = [n for n in z.namelist()
                     if n.lower().endswith((".srt", ".ass", ".ssa", ".vtt"))]
            if not names:
                raise ValueError("压缩包里没有可用的文本字幕")
            names.sort(key=lambda n: (not n.lower().endswith(".srt"), n))
            return z.read(names[0])
    return raw


def download(cand: Dict[str, Any], cfg: Dict[str, Any]) -> bytes:
    """下载字幕字节；需要临时换链接的源先 resolve，再统一校验白名单。"""
    scfg = _sub_cfg(cfg)
    mod = sources.module_of(str(cand.get("provider") or ""))
    url = cand.get("url") or ""
    if mod is not None and hasattr(mod, "resolve"):
        url = mod.resolve(cand, scfg)
    guard_url(url, cand)
    raw = common.request(url, scfg, binary=True, timeout=30,
                         provider=str(cand.get("provider") or ""))
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("字幕内容为空或超过 4 MB")
    raw = _maybe_decompress(raw)
    if len(raw) > MAX_BYTES:
        raise ValueError("解压后的字幕超过 4 MB")
    head = raw[:256].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html")):
        raise ValueError("下载到的是网页而不是字幕文件")
    return raw
