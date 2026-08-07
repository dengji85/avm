# -*- coding: utf-8 -*-
"""后台任务状态（扫描 / 抓取），供前端轮询进度。"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List


class Job:
    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.running = False
            self.cancelled = False
            self.phase = "idle"
            self.total = 0
            self.done = 0
            self.current = ""
            self.counters: Dict[str, int] = {}
            self.errors: List[str] = []
            self.logs: List[Dict[str, Any]] = []
            self.message = ""
            self.started_at = 0.0
            self.ended_at = 0.0

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self.reset()
            self.running = True
            self.phase = "starting"
            self.started_at = time.time()
            return True

    def finish(self, message: str = "") -> None:
        with self._lock:
            self.running = False
            self.phase = "done"
            self.ended_at = time.time()
            if message:
                self.message = message

    def cancel(self) -> None:
        with self._lock:
            if self.running:
                self.cancelled = True
                self.message = "正在取消…"

    def bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.counters[key] = self.counters.get(key, 0) + n

    def log(self, msg: str, level: str = "info", code: str = "") -> None:
        """追加一条滚动日志（最多保留最近 200 条）。

        level 取 info / warn / error；前端只展示 warn/error 作为「未命中 / 错误明细」。
        """
        with self._lock:
            self.logs.append({
                "t": time.strftime("%H:%M:%S"),
                "level": level,
                "msg": str(msg)[:300],
                "code": code or "",
            })
            if len(self.logs) > 200:
                del self.logs[0: len(self.logs) - 200]

    def tick(self, current: str = "") -> None:
        with self._lock:
            self.done += 1
            if current:
                self.current = current

    def update(self, **kw) -> None:
        """通用进度上报：total/done/phase/current/message 直接设值；
        其余键视为计数器，按传入值（绝对累计）设置，便于调用方直接上报累计数。"""
        with self._lock:
            for k, v in kw.items():
                if k in ("running", "cancelled", "phase", "total", "done", "current", "message"):
                    setattr(self, k, v)
                else:
                    self.counters[k] = v

    def error(self, msg: str) -> None:
        with self._lock:
            if len(self.errors) < 100:
                self.errors.append(str(msg)[:300])
            self.log(msg, "error")

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            elapsed = (self.ended_at or time.time()) - self.started_at if self.started_at else 0
            percent = round(self.done / self.total * 100, 1) if self.total else (100.0 if self.phase == "done" else 0.0)
            return {
                "name": self.name,
                "running": self.running,
                "cancelled": self.cancelled,
                "phase": self.phase,
                "total": self.total,
                "done": self.done,
                "percent": percent,
                "current": self.current,
                "counters": dict(self.counters),
                "errors": list(self.errors[-20:]),
                "error_count": len(self.errors),
                "logs": [
                    {"t": l["t"], "level": l["level"], "msg": l["msg"], "code": l["code"]}
                    for l in self.logs if l["level"] in ("warn", "error")
                ],
                "message": self.message,
                "elapsed": round(elapsed, 1),
            }


SCAN = Job("scan")
SCRAPE = Job("scrape")
