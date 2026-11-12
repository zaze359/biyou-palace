"""运行时事件环形缓冲 —— `GET /v1/logs` 的唯一数据源。

只记录节点**显式写入**的白名单事件（启动/预热/鉴权失败/并发拒绝/上游错误/
客户端断连/未捕获异常）。写入前统一脱敏：隐去 shared_token 与常见绝对文件
路径，并截断超长消息。本模块不接触请求 body、上游请求头与日志文件路径。
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

CAPACITY = 200
_LEVELS = ("INFO", "WARNING", "ERROR")

_ring: deque[dict[str, Any]] = deque(maxlen=CAPACITY)
_total = 0
_token: str | None = None
_sink: "_FileSink | None" = None
_sink_lock = threading.Lock()

# 只匹配常见绝对路径前缀，避免把 URL 里的斜杠误伤
_ABS_PATH_RE = re.compile(r"(?:/Users|/home|/tmp|/private|/var|/opt|/root)/[^\s,，;)\"']*")


class _FileSink:
    """按大小轮转的 JSONL 落盘；任何 IO 异常都返回 False，由调用方降级。"""

    def __init__(self, path: str, max_bytes: int, backup_count: int) -> None:
        self.path = Path(path)
        self.max_bytes = max(1, int(max_bytes))
        self.backup_count = max(0, int(backup_count))
        self._handle = None

    def _open(self) -> Any:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return self.path.open("a", encoding="utf-8")

    def _rotate(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self.backup_count == 0:
            self.path.unlink(missing_ok=True)
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            target = self.path.with_name(f"{self.path.name}.{index + 1}")
            if source.exists():
                os.replace(source, target)
        if self.path.exists():
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))

    def write(self, line: str) -> bool:
        payload = (line + "\n").encode("utf-8")
        try:
            if self._handle is None:
                self._handle = self._open()
            if self._handle.tell() + len(payload) > self.max_bytes:
                self._rotate()
                self._handle = self._open()
            self._handle.write(payload.decode("utf-8"))
            self._handle.flush()
            return True
        except Exception:  # noqa: BLE001 - 落盘失败必须降级，不能抛给业务
            try:
                if self._handle is not None:
                    self._handle.close()
            except Exception:  # noqa: BLE001
                pass
            self._handle = None
            return False


def configure(
    token: str | None = None,
    *,
    file_path: str | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
) -> None:
    """登记脱敏 token 与可选落盘目标（启动时由 lifespan 调用）。"""
    global _token, _sink
    _token = token or None
    with _sink_lock:
        _sink = _FileSink(file_path, max_bytes, backup_count) if file_path else None


def _scrub(message: str) -> str:
    text = message
    if _token:
        text = text.replace(_token, "***")
    return _ABS_PATH_RE.sub("<path>", text)


def _append(event: dict[str, Any]) -> None:
    global _total, _sink
    _ring.append(event)
    _total += 1
    with _sink_lock:
        sink = _sink
    if sink is None:
        return
    if not sink.write(json.dumps(event, ensure_ascii=False)):
        # 落盘失败：本进程内彻底降级为仅内存，并留下一条可观测告警
        with _sink_lock:
            _sink = None
        _ring.append(
            {
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "level": "WARNING",
                "event": "logfile",
                "message": "日志落盘失败，已降级为仅内存环形缓冲",
            }
        )
        _total += 1


def record(level: str, event: str, message: str) -> None:
    """写入一条事件。level 非法时按 INFO 处理。"""
    lvl = level.upper()
    if lvl not in _LEVELS:
        lvl = "INFO"
    _append(
        {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "level": lvl,
            "event": event,
            "message": _scrub(str(message))[:500],
        }
    )


def recent(limit: int = 50) -> dict[str, Any]:
    """返回最近 limit 条事件（limit 会被夹到 1..CAPACITY）。"""
    n = max(1, min(int(limit), CAPACITY))
    return {"lines": list(_ring)[-n:], "limit": n, "capacity": CAPACITY, "total": _total}


def clear() -> None:
    """清空缓冲与落盘配置（测试用）。"""
    global _total, _token, _sink
    _ring.clear()
    _total = 0
    _token = None
    with _sink_lock:
        if _sink is not None and _sink._handle is not None:
            try:
                _sink._handle.close()
            except Exception:  # noqa: BLE001
                pass
        _sink = None
