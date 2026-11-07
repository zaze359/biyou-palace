"""运行时事件环形缓冲 —— `GET /v1/logs` 的唯一数据源。

只记录节点**显式写入**的白名单事件（启动/预热/鉴权失败/并发拒绝/上游错误/
客户端断连/未捕获异常）。写入前统一脱敏：隐去 shared_token 与常见绝对文件
路径，并截断超长消息。本模块不接触请求 body、上游请求头与日志文件路径。
"""
from __future__ import annotations

import re
from collections import deque
from datetime import datetime
from typing import Any

CAPACITY = 200
_LEVELS = ("INFO", "WARNING", "ERROR")

_ring: deque[dict[str, Any]] = deque(maxlen=CAPACITY)
_total = 0
_token: str | None = None

# 只匹配常见绝对路径前缀，避免把 URL 里的斜杠误伤
_ABS_PATH_RE = re.compile(r"(?:/Users|/home|/tmp|/private|/var|/opt|/root)/[^\s,，;)\"']*")


def configure(token: str | None = None) -> None:
    """登记需要脱敏的 token（启动时由 lifespan 调用）。"""
    global _token
    _token = token or None


def _scrub(message: str) -> str:
    text = message
    if _token:
        text = text.replace(_token, "***")
    return _ABS_PATH_RE.sub("<path>", text)


def record(level: str, event: str, message: str) -> None:
    """写入一条事件。level 非法时按 INFO 处理。"""
    global _total
    lvl = level.upper()
    if lvl not in _LEVELS:
        lvl = "INFO"
    _ring.append(
        {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "level": lvl,
            "event": event,
            "message": _scrub(str(message))[:500],
        }
    )
    _total += 1


def recent(limit: int = 50) -> dict[str, Any]:
    """返回最近 limit 条事件（limit 会被夹到 1..CAPACITY）。"""
    n = max(1, min(int(limit), CAPACITY))
    return {"lines": list(_ring)[-n:], "limit": n, "capacity": CAPACITY, "total": _total}


def clear() -> None:
    """清空缓冲（测试用）。"""
    global _total, _token
    _ring.clear()
    _total = 0
    _token = None
