# -*- coding: utf-8 -*-
"""刮削失败原因诊断：把原始异常/拦截页翻译成「现象 + 你该怎么做」。

设计目标：
- 输入是 scraper 逐源捕获到的 (provider, status, raw_reason) 列表。
- 输出是结构化的诊断项：归类、人话描述、推荐操作、可一键执行的动作类型。
- 纯函数，无副作用，便于单测与前端直接消费。

归类（kind）语义约定：
- blocked   : 数据源返回了反爬/人机验证页（Cloudflare / JavBus / av-wiki Loader）
              —— 不是片不存在，临时拦截，可重试/填 Cookie。
- neterr    : 网络超时、代理抖动、5xx 等网络层错误 —— 可重试。
- miss      : 所有源都明确「无此番号」（404 / 列表无匹配）—— 真缺，建议手动补 nfo。
- parse_err : 页面结构变化导致解析崩溃 / 字段异常 —— 需报告或等更新。
- code_issue: 番号可能被误解析（如素人/无码特殊格式）—— 建议手动指定番号重抓。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

# 各 kind 的展示信息（颜色在前端按此映射）
KIND_META = {
    "blocked":   {"label": "反爬拦截", "color": "red",    "retryable": True},
    "neterr":    {"label": "网络错误", "color": "orange", "retryable": True},
    "miss":      {"label": "源无此片", "color": "gray",   "retryable": False},
    "parse_err": {"label": "解析失败", "color": "purple", "retryable": False},
    "code_issue": {"label": "番号异常", "color": "blue",  "retryable": True},
    "ok":        {"label": "成功",     "color": "green",  "retryable": False},
}

# 针对每种 kind 的推荐操作（action 是前端可触发的一键动作类型）
_RECO_BY_KIND = {
    "blocked": (
        "数据源被反爬拦截，并非影片不存在。可填入该站的 Cookie（或开启 CDP）后重试。",
        "fill_cookie",
    ),
    "neterr": (
        "网络层错误（超时/代理抖动/源服务端异常），稍后重试通常即可恢复。",
        "retry_neterr",
    ),
    "miss": (
        "所有已启用的数据源都查不到这个番号，可能是素人/无码特殊编号或站外片。",
        "manual_nfo",
    ),
    "parse_err": (
        "数据源页面结构变化导致解析失败，需要更新抓取规则。",
        "report",
    ),
    "code_issue": (
        "番号可能被误解析（常见于素人/无码/分段命名），可手动指定番号后重抓。",
        "fix_code",
    ),
}


def classify_source(provider: str, status: str, raw_reason: str = "") -> Dict[str, str]:
    """对单个数据源的刮削结果做归类。

    入参 status 来自 scraper 的 "_classify_error" 结果
    （ok / miss / neterr / blocked / parse_err / code_issue 等）。
    raw_reason 是该源捕获到的原始描述（异常文本或 detect_blocker 输出）。
    """
    status = (status or "").lower()
    raw_reason = (raw_reason or "").strip()

    if status in ("ok", "hit"):
        return {"kind": "ok", "reason": raw_reason or "命中", "provider": provider}

    if status == "blocked" or _looks_blocked(raw_reason):
        reason = raw_reason or "被反爬/人机验证页拦截"
        return {"kind": "blocked", "reason": reason, "provider": provider}

    if status == "code_issue":
        return {"kind": "code_issue", "reason": raw_reason or "番号解析异常", "provider": provider}

    if status == "parse_err":
        return {"kind": "parse_err", "reason": raw_reason or "源页面结构变化，解析失败", "provider": provider}

    if status in ("neterr", "error", "net"):
        reason = _humanize_neterr(raw_reason)
        return {"kind": "neterr", "reason": reason, "provider": provider}

    # 默认（miss / 其他未命中）
    reason = raw_reason or "所有数据源均未命中此番号"
    return {"kind": "miss", "reason": reason, "provider": provider}


def _looks_blocked(raw_reason: str) -> bool:
    """从原因文本反推是否被反爬拦截（兜底，scraper 已优先用 status 标注）。"""
    key = ("cloudflare", "人机验证", "driver-verify", "age verification",
           "loader", "正在验证", "cf_clearance", "challenge-platform")
    low = raw_reason.lower()
    return any(k in low for k in key)


def _humanize_neterr(raw_reason: str) -> str:
    """把网络异常的具体类名翻译成人话。"""
    r = raw_reason
    if not r:
        return "网络请求失败"
    low = r.lower()
    if "timeout" in low:
        return "请求超时（数据源响应过慢或网络不稳）"
    if "connection" in low or "connect" in low:
        return "连接失败（代理/网络不可达）"
    if "5" in r and ("500" in r or "502" in r or "503" in r or "504" in r):
        return "数据源服务端异常（5xx）"
    if "429" in r:
        return "触发数据源限流（429），请降低频率后重试"
    if "proxy" in low:
        return "代理配置异常"
    return f"网络错误：{r}"


def diagnose(detail: Any) -> Dict[str, Any]:
    """对一条 scrape_logs.detail（JSON 或 list）做整体诊断。

    返回：
    {
      "summary_kind": "blocked" | "neterr" | "miss" | "mixed" | "ok",
      "headline":     "人类可读的一句话总结",
      "sources":      [ {provider, kind, reason, color, retryable}, ... ],
      "action":       "retry_neterr" | "fill_cookie" | "manual_nfo" | ... | "",
      "recommend":    "针对该片的建议文案",
    }
    """
    sources: List[Dict[str, Any]] = _parse_detail(detail)
    if not sources:
        return {
            "summary_kind": "miss",
            "headline": "无逐源明细记录",
            "sources": [],
            "action": "",
            "recommend": "无法定位具体原因，建议手动重抓。",
        }

    # 归并逐源诊断
    per = []
    kinds: set = set()
    for s in sources:
        d = classify_source(
            s.get("provider", "?"),
            s.get("status", "miss"),
            s.get("reason", ""),
        )
        meta = KIND_META.get(d["kind"], KIND_META["miss"])
        per.append({
            "provider": d["provider"],
            "kind": d["kind"],
            "label": meta["label"],
            "color": meta["color"],
            "reason": d["reason"],
            "retryable": meta["retryable"],
            "elapsed_ms": s.get("elapsed_ms", 0),
        })
        kinds.add(d["kind"])

    # 汇总归类：优先取最"可操作"的那类
    summary_kind, action, recommend = _summarize(kinds, sources)
    headline = _headline(summary_kind, kinds, len(sources))

    return {
        "summary_kind": summary_kind,
        "headline": headline,
        "sources": per,
        "action": action,
        "recommend": recommend,
    }


def _parse_detail(detail: Any) -> List[Dict[str, Any]]:
    if isinstance(detail, list):
        return detail
    if isinstance(detail, str) and detail.strip():
        try:
            obj = json.loads(detail)
            return obj if isinstance(obj, list) else []
        except Exception:
            return []
    return []


def _summarize(kinds: set, sources: List[Dict[str, Any]]):
    """根据逐源 kind 集合，决定整体归类、推荐动作与文案。"""
    if "blocked" in kinds:
        _, action = _RECO_BY_KIND["blocked"]
        return "blocked", action, _RECO_BY_KIND["blocked"][0]
    if "neterr" in kinds:
        _, action = _RECO_BY_KIND["neterr"]
        return "neterr", action, _RECO_BY_KIND["neterr"][0]
    if kinds == {"miss"}:
        _, action = _RECO_BY_KIND["miss"]
        return "miss", action, _RECO_BY_KIND["miss"][0]
    if "code_issue" in kinds and kinds <= {"code_issue", "miss"}:
        _, action = _RECO_BY_KIND["code_issue"]
        return "code_issue", action, _RECO_BY_KIND["code_issue"][0]
    if "parse_err" in kinds:
        _, action = _RECO_BY_KIND["parse_err"]
        return "parse_err", action, _RECO_BY_KIND["parse_err"][0]
    # 混合情况（如部分源命中、部分未命中但非错误）
    if kinds - {"ok"}:
        return "mixed", "", "部分数据源成功、部分失败，可换源或针对性重试失败源。"
    return "miss", "", _RECO_BY_KIND["miss"][0]


def _headline(summary_kind: str, kinds: set, n: int) -> str:
    labels = {KIND_META[k]["label"] for k in kinds if k in KIND_META}
    if summary_kind == "ok":
        return "刮削成功"
    if summary_kind == "mixed":
        return f"{n} 个数据源混合结果：{ ' / '.join(labels) }"
    name = KIND_META.get(summary_kind, {}).get("label", "失败")
    if len(kinds) == 1:
        return f"全部数据源：{name}"
    return f"主要失败原因：{name}（涉及 { ' / '.join(labels) }）"


def group_failures(diagnoses: List[Dict[str, Any]]) -> Dict[str, int]:
    """对一批诊断做归类计数，供面板总览卡使用。"""
    counter: Dict[str, int] = {k: 0 for k in KIND_META if k != "ok"}
    for d in diagnoses:
        k = d.get("summary_kind", "miss")
        if k in counter:
            counter[k] += 1
    return counter
