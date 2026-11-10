# -*- coding: utf-8 -*-
"""在线来源的清晰度 / 中文字幕探测：结论带依据，失败只记说明不影响播放。

能稳定做到的：

* **清晰度**：HLS master 播放列表的 ``EXT-X-STREAM-INF`` 自带
  ``RESOLUTION`` / ``BANDWIDTH``，取一次播放列表就能拿到（123av 等站点实测
  主列表只有 73 字节就带 ``RESOLUTION=854x480``）。子列表（media playlist）
  没有声明时，用 ``ffprobe``（``ext_components.ffprobe_exe``）探一次宽高码率，
  单条约 4~15 秒，放后台跑。
* **HLS 字幕轨**：``EXT-X-MEDIA:TYPE=SUBTITLES`` / ``SUBTITLES=`` 组可直接判定。
  注意很多站点把字幕做成页面外挂 vtt，不经过播放列表，超出这里的判定范围。

做不到（或代价过高，不做）、因此**只用 unknown 表示，不猜**：

* 站点页面上的「中文字幕」标签：123av / jable 走浏览器插件入库，服务端没有
  对应 provider，页面本身还需浏览器会话才能过 Cloudflare，默认不抓；
  只有来源标题里自带的标注（「中文字幕」「中字」「Chinese Subtitle」）作为弱证据。
* 烧录在画面里的字幕：只能抽帧 + OCR，逐片成本以分钟计，不做默认全量。
* 分片用伪装扩展名的站点（123av 的 ``.css`` 分片）：ffmpeg 的 HLS 解析器
  会直接拒绝（``not in allowed_segment_extensions``），此时退回播放列表声明，
  声明里没有分辨率就记 unknown。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import stream_verify as sv
from .config import load_config
from .db import db, query_all, query_one
from .threadpool import submit_long, submit_task

#: 标题里的中文字幕标注（站点自己写在标题 / 标签上的，属于站点证据）
_TITLE_ZH = re.compile(
    r"中文字幕|中字|中文|简中|繁中|简体字幕|繁体字幕|双语字幕|字幕版"
    r"|chinese\s*sub(?:title)?s?|chinese\s*subtitled|\bchs\b|\bcht\b", re.I)
_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_ZH_LANG = re.compile(r"^(zh|zho|chi|cmn|chs|cht)", re.I)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def cfg() -> Dict[str, Any]:
    return dict(load_config().get("stream") or {})


def _attrs(line: str) -> Dict[str, str]:
    """解析 ``#EXT-X-...:KEY=VALUE,KEY="VALUE"`` 里的属性表。"""
    head, _, body = line.partition(":")
    del head
    out: Dict[str, str] = {}
    for key, val in _ATTR.findall(body):
        out[key.upper()] = val.strip().strip('"')
    return out


def _is_zh(lang: str, name: str = "") -> bool:
    lang = str(lang or "").strip()
    if lang and _ZH_LANG.match(lang):
        return True
    return bool(re.search(r"中|简|繁", str(name or "")))


def parse_playlist(text: str) -> Dict[str, Any]:
    """解析 HLS 播放列表：变体清晰度 + 字幕轨（都不联网）。"""
    variants: List[Dict[str, int]] = []
    subs: List[Dict[str, str]] = []
    master = False
    media = False
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("#EXT-X-STREAM-INF:"):
            master = True
            a = _attrs(line)
            w = h = 0
            m = re.match(r"^(\d+)x(\d+)$", a.get("RESOLUTION", ""))
            if m:
                w, h = int(m.group(1)), int(m.group(2))
            bw = a.get("AVERAGE-BANDWIDTH") or a.get("BANDWIDTH") or "0"
            try:
                variants.append({"width": w, "height": h, "bandwidth": int(bw)})
            except ValueError:
                variants.append({"width": w, "height": h, "bandwidth": 0})
        elif upper.startswith("#EXT-X-MEDIA:"):
            a = _attrs(line)
            if a.get("TYPE", "").upper() == "SUBTITLES":
                subs.append({"lang": a.get("LANGUAGE", ""),
                             "name": a.get("NAME", ""),
                             "uri": a.get("URI", "")})
        elif upper.startswith("#EXTINF"):
            media = True
    best = max(variants, key=lambda v: (v["height"], v["bandwidth"]), default=None)
    return {"master": master, "media": media, "variants": variants,
            "best": best or {}, "subs": subs}


def _ffprobe(url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    """ffprobe 探宽高：返回 {ok,width,height,note}；任何失败都只降级不抛。"""
    from .ext_components import ffprobe_exe
    exe = ffprobe_exe()
    found = os.path.exists(exe) if os.path.dirname(exe) else bool(shutil.which(exe))
    if not found:
        return {"ok": False, "note": "未找到 ffprobe，跳过清晰度探测"}
    hdr = "".join("%s: %s\r\n" % (k, v) for k, v in headers.items()
                  if k in ("Referer", "Origin", "User-Agent"))
    cmd = [exe, "-v", "error", "-print_format", "json",
           "-show_entries", "stream=codec_type,width,height",
           "-probesize", "300000", "-analyzeduration", "3000000",
           "-headers", hdr]
    proxies = sv.upstream_proxies(url)
    proxy = (proxies or {}).get("https") or (proxies or {}).get("http") or ""
    if proxy:
        cmd += ["-http_proxy", proxy]
    cmd.append(url)
    try:
        timeout = max(5, int(cfg().get("probe_timeout") or 30))
        run = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "note": "ffprobe 超时（%ss），清晰度记为未知" % timeout}
    except Exception as exc:
        return {"ok": False, "note": "ffprobe 无法执行（%s）" % type(exc).__name__}
    try:
        data = json.loads((run.stdout or b"").decode("utf-8", "ignore") or "{}")
    except ValueError:
        data = {}
    for st in data.get("streams") or []:
        if str(st.get("codec_type")) == "video" and st.get("width"):
            return {"ok": True, "width": int(st["width"]), "height": int(st["height"])}
    err = (run.stderr or b"").decode("utf-8", "ignore")
    if "allowed_segment_extensions" in err:
        return {"ok": False,
                "note": "分片用伪装扩展名，ffprobe 无法解析（以播放列表声明为准）"}
    first = next((x.strip() for x in err.splitlines() if x.strip()), "")
    return {"ok": False, "note": ("ffprobe 未取到画面信息" + ("：" + first[:80] if first else ""))}


def _apply_probe(conn, source_id: int, data: Dict[str, Any]) -> None:
    conn.execute(
        "UPDATE movie_sources SET media_width = ?, media_height = ?, media_bandwidth = ?, "
        "media_source = ?, media_probe_note = ?, media_probe_at = ?, sub_zh = ?, "
        "sub_evidence = ?, sub_langs = ?, updated_at = datetime('now','localtime') "
        "WHERE id = ?",
        (int(data.get("width") or 0), int(data.get("height") or 0),
         int(data.get("bandwidth") or 0), str(data.get("media_source") or ""),
         str(data.get("note") or "")[:200], _now(), str(data.get("sub_zh") or "unknown"),
         str(data.get("sub_evidence") or ""), str(data.get("sub_langs") or "")[:100],
         int(source_id)),
    )


def probe_source(source_id: int) -> Dict[str, Any]:
    """探测单条来源：写清晰度 + 字幕判定（含依据）。失败只记说明。"""
    with db() as conn:
        row = query_one(conn, "SELECT id, movie_id, play_url, page_url, title, kind, "
                              "play_headers FROM movie_sources WHERE id = ?", (int(source_id),))
    if not row:
        return {"ok": False, "message": "来源不存在"}
    url = str(row["play_url"] or "")
    title = str(row["title"] or "")
    if not url:
        with db() as conn:
            conn.execute(
                "UPDATE movie_sources SET media_probe_note = ? WHERE id = ?",
                ("没有播放地址，无法探测清晰度", int(source_id)))
        return {"ok": False, "message": "没有播放地址"}
    if sv._url_problem(url)[0]:                       # noqa: SLF001 同模块沿用来源校验
        return {"ok": False, "message": "播放地址不合法，已跳过探测"}

    headers = sv.upstream_headers(url, source_id=int(source_id))
    res = sv._fetch(url, limit=512 * 1024, headers=headers)   # noqa: SLF001
    data: Dict[str, Any] = {"sub_zh": "unknown", "sub_evidence": ""}
    playlist_ok = bool(res["ok"] and str(res["text"]).lstrip().startswith("#EXTM3U"))

    if playlist_ok:
        info = parse_playlist(res["text"])
        best = info.get("best") or {}
        if best.get("width"):
            data.update(width=best["width"], height=best["height"],
                        bandwidth=best.get("bandwidth") or 0, media_source="manifest")
        subs = info.get("subs") or []
        langs = [s.get("lang") or s.get("name") or "" for s in subs]
        if subs:
            data["sub_langs"] = ",".join([x for x in langs if x])
            if any(_is_zh(s.get("lang", ""), s.get("name", "")) for s in subs):
                data.update(sub_zh="zh", sub_evidence="hls_track")
            elif all(x for x in langs):
                # 有字幕轨但语言明确不是中文：可解释的「无中文字幕」
                data.update(sub_zh="none", sub_evidence="hls_track")
            else:
                data.update(sub_zh="unknown", sub_evidence="hls_track")
        elif _TITLE_ZH.search(title):
            data.update(sub_zh="zh", sub_evidence="site_label")
        elif info.get("master") or info.get("media"):
            # 播放列表里没有任何字幕轨，标题也没有标注：记「未发现」，
            # 依据写 none，前端据此显示「未发现中文字幕」而不是「无字幕」。
            data.update(sub_zh="none", sub_evidence="none")
        if not data.get("width"):
            fp = _ffprobe(url, headers)
            if fp.get("ok"):
                data.update(width=fp["width"], height=fp["height"], media_source="ffprobe")
            else:
                data["note"] = fp.get("note") or "清晰度未知"
    else:
        # 取不到播放列表（多为防盗链头缺失 / 签名过期）：不覆盖已有结论，
        # 也不写 media_probe_at，等下次验证通过后再探一次。
        status = int(res.get("status") or 0)
        with db() as conn:
            conn.execute(
                "UPDATE movie_sources SET media_probe_note = ? WHERE id = ?",
                ("取不到播放列表（HTTP %d），清晰度与字幕暂不判定" % status, int(source_id)))
        return {"ok": False, "message": "取不到播放列表（HTTP %d）" % status}

    with db() as conn:
        _apply_probe(conn, source_id, data)
    return {"ok": True, "source_id": int(source_id), **data}


# ----------------------------------------------------------------- 后台批量

_JOB: Dict[str, Any] = {"running": False, "total": 0, "done": 0, "percent": 0,
                        "ok": 0, "failed": 0, "message": "", "cancel": False}
_JOB_LOCK = threading.Lock()
_PENDING_LOCK = threading.Lock()
_PENDING: set = set()


def _needs_probe(conn, source_id: int) -> bool:
    row = query_one(conn, "SELECT media_probe_at FROM movie_sources WHERE id = ?",
                    (int(source_id),))
    return bool(row) and not str(row["media_probe_at"] or "").strip()


def submit_probe(source_id: int) -> Dict[str, Any]:
    """入库 / 验证后自动探测一条来源（幂等；只探从没探过的）。"""
    if not bool(cfg().get("probe_media", True)):
        return {"ok": True, "queued": False}
    sid = int(source_id or 0)
    if not sid:
        return {"ok": True, "queued": False}
    with db() as conn:
        if not _needs_probe(conn, sid):
            return {"ok": True, "queued": False}
    with _PENDING_LOCK:
        if sid in _PENDING:
            return {"ok": True, "queued": False}
        _PENDING.add(sid)

    def run():
        try:
            probe_source(sid)
        except Exception:
            pass
        finally:
            with _PENDING_LOCK:
                _PENDING.discard(sid)

    submit_task(run)
    return {"ok": True, "queued": True}


def state() -> Dict[str, Any]:
    with _JOB_LOCK:
        return dict(_JOB)


def cancel() -> Dict[str, Any]:
    with _JOB_LOCK:
        if _JOB["running"]:
            _JOB["cancel"] = True
            _JOB["message"] = "正在停止…"
    return state()


def start(limit: int = 300, force: bool = False) -> Dict[str, Any]:
    """后台批量探测（可取消、有进度；重复点击返回同一任务）。"""
    with _JOB_LOCK:
        if _JOB["running"]:
            return state()
        _JOB.update(running=True, total=0, done=0, percent=0, ok=0, failed=0,
                    message="正在准备…", cancel=False)
    with db() as conn:
        sql = ("SELECT id FROM movie_sources WHERE available = 1 AND play_url <> '' "
               + ("" if force else "AND media_probe_at = '' ")
               + "ORDER BY media_probe_at ASC, id DESC LIMIT ?")
        ids = [int(r["id"]) for r in query_all(conn, sql, (max(1, int(limit)),))]
    with _JOB_LOCK:
        _JOB["total"] = len(ids)
        _JOB["message"] = "共 %d 条来源待探测" % len(ids) if ids else "没有需要探测的来源"
        if not ids:
            _JOB["running"] = False
    if ids:
        submit_long(_run, ids)
    return state()


def _run(ids: List[int]) -> None:
    try:
        for n, sid in enumerate(ids, 1):
            with _JOB_LOCK:
                if _JOB["cancel"]:
                    _JOB["message"] = "已取消（完成 %d/%d）" % (n - 1, len(ids))
                    break
                _JOB["message"] = "正在探测第 %d/%d 条" % (n, len(ids))
            try:
                res = probe_source(sid)
                ok = bool(res.get("ok"))
            except Exception:
                ok = False
            with _JOB_LOCK:
                _JOB["done"] = n
                _JOB["ok" if ok else "failed"] += 1
                _JOB["percent"] = int(n * 100 / max(1, len(ids)))
    finally:
        with _JOB_LOCK:
            _JOB["running"] = False
            _JOB["cancel"] = False
            if not _JOB["message"].startswith("已取消"):
                _JOB["message"] = "探测完成：成功 %d，失败 %d" % (_JOB["ok"], _JOB["failed"])
