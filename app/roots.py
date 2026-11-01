# -*- coding: utf-8 -*-
"""存储根探针：判断媒体库目录当前是否真的可用。

**解决什么问题**：媒体库常放外接盘 / NAS。盘没插或没挂载时，配置里的 root
依然存在，但目录读不到。旧实现会把这些根下的所有影片一次性标成「文件缺失」，
而 `auto_cleanup` 在宽限期（默认 30 天）后就会真删记录 —— 等于「拔盘一个月，
库没了」。本模块负责回答一个前置问题：**这个根现在能不能用来判断文件缺失？**

判定规则（保守优先）：
* 目录不存在                     → ``offline``（没插盘 / 没挂载 / 改了盘符）
* 目录存在但不可枚举（权限/IO）    → ``offline``
* 目录存在、可枚举、但一个条目都没有 → ``suspicious``（挂载点是空的 / 换了盘）
* 其它                           → ``ok``

只有 ``ok`` 的根才允许参与「标记缺失」与「清空记录」；其余根下的数据一律原样
保留，等盘回来再判定。宁可少标一次，也不能误删。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

STATE_OK = "ok"
STATE_OFFLINE = "offline"
STATE_SUSPICIOUS = "suspicious"


@dataclass
class RootState:
    """单个存储根的探测结果。"""

    path: str
    state: str = STATE_OK
    reason: str = ""
    entries: int = 0
    checked_at: str = ""
    last_ok_at: str = ""

    @property
    def usable(self) -> bool:
        return self.state == STATE_OK

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "state": self.state,
            "reason": self.reason,
            "entries": int(self.entries or 0),
            "checked_at": self.checked_at,
            "last_ok_at": self.last_ok_at,
            "usable": self.usable,
        }


def normalize_prefix(path: str) -> str:
    """统一成「小写、正斜杠、无尾斜杠」，用于前缀比较。"""
    return str(path or "").replace("\\", "/").lower().rstrip("/")


def under_any(path: str, prefixes: Sequence[str]) -> bool:
    """路径是否落在任一前缀之下（前缀需先 normalize_prefix）。"""
    if not path or not prefixes:
        return False
    lp = normalize_prefix(path)
    return any(lp == p or lp.startswith(p + "/") for p in prefixes)


def probe_root(path: str) -> RootState:
    """探测单个根。只做「存在 + 可枚举 + 非空」三件事，毫秒级。"""
    p = str(path or "").strip()
    if not p:
        return RootState(p, STATE_OFFLINE, "未配置路径")
    if not os.path.isdir(p):
        return RootState(p, STATE_OFFLINE, "目录不存在（磁盘未挂载或盘符已变）")
    try:
        n = 0
        with os.scandir(p) as it:
            for _ in it:
                n += 1
                break
    except OSError as e:
        return RootState(p, STATE_OFFLINE, "无法读取目录（%s）" % type(e).__name__)
    if n == 0:
        # 空目录：可能挂载失败（空挂载点），也可能用户真的清空了库。
        # 两种情况都不该自动判缺失 —— 交给人确认。
        return RootState(p, STATE_SUSPICIOUS, "目录为空（挂载失败或换了盘？）")
    return RootState(p, STATE_OK, "")


def probe_roots(roots: Optional[Iterable[str]],
                conn: Optional[sqlite3.Connection] = None) -> Dict[str, RootState]:
    """批量探测；传 conn 时把结果落库（供 UI 显示「上次可用时间」）。"""
    out: Dict[str, RootState] = {}
    for r in roots or []:
        key = str(r or "").strip()
        if not key:
            continue
        out[key] = probe_root(key)
    if conn is not None:
        try:
            save_states(conn, out)
        except Exception:
            pass  # 落库失败不影响判定
    return out


def usable_prefixes(states: Dict[str, RootState]) -> List[str]:
    return [normalize_prefix(p) for p, s in states.items() if s.usable]


def down_prefixes(states: Dict[str, RootState]) -> List[str]:
    return [normalize_prefix(p) for p, s in states.items() if not s.usable]


def down_roots(states: Dict[str, RootState]) -> List[RootState]:
    return [s for s in states.values() if not s.usable]


def states_as_list(states: Dict[str, RootState]) -> List[Dict[str, Any]]:
    return [s.to_dict() for s in states.values()]


def save_states(conn: sqlite3.Connection, states: Dict[str, RootState]) -> None:
    """把探测结果写进 storage_roots（UPSERT，保留 last_ok_at）。"""
    for path, st in states.items():
        conn.execute(
            """INSERT INTO storage_roots(path, state, reason, entries, checked_at, last_ok_at)
                   VALUES (?, ?, ?, ?, datetime('now','localtime'),
                           CASE WHEN ? = 'ok' THEN datetime('now','localtime') ELSE '' END)
               ON CONFLICT(path) DO UPDATE SET
                   state      = excluded.state,
                   reason     = excluded.reason,
                   entries    = excluded.entries,
                   checked_at = excluded.checked_at,
                   last_ok_at = CASE WHEN excluded.state = 'ok'
                                     THEN excluded.checked_at
                                     ELSE storage_roots.last_ok_at END""",
            (path, st.state, st.reason, int(st.entries or 0), st.state),
        )


def load_states(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """读最近一次落库的根状态（可能为空：老库还没探过）。"""
    rows = conn.execute(
        "SELECT path, state, reason, entries, checked_at, last_ok_at "
        "FROM storage_roots ORDER BY path"
    ).fetchall()
    return [dict(r) for r in rows]


__all__ = [
    "STATE_OK", "STATE_OFFLINE", "STATE_SUSPICIOUS", "RootState",
    "normalize_prefix", "under_any", "probe_root", "probe_roots",
    "usable_prefixes", "down_prefixes", "down_roots", "states_as_list",
    "save_states", "load_states",
]
