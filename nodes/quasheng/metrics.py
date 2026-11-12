"""准圣节点 Prometheus 指标（进程内，手写文本 exposition）。

设计约束：
- 不引入新依赖；指标只是旁路观测，任何采集/渲染异常都必须被吞掉，
  绝不能影响业务请求或日志。
- 标签只包含模型 id、上游 id、状态码等非敏感信息；不含 token、base_url、
  文件路径。
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any

_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
_lock = threading.Lock()

_requests: dict[tuple[str, str, str], int] = {}
_model_requests: dict[tuple[str, str, str], int] = {}
_duration: dict[tuple[str, str, str], "Histogram"] = {}
_first_byte: dict[tuple[str, str, str], "Histogram"] = {}
_discovery: dict[tuple[str, str], int] = {}
_last_success: dict[str, float] = {}
_fallbacks: dict[tuple[str, str], int] = {}
_exhausted: dict[str, int] = {}
_inflight = 0


@dataclass
class Histogram:
    counts: list[int] = field(default_factory=lambda: [0] * len(_BUCKETS))
    total: int = 0
    sum_value: float = 0.0

    def observe(self, value: float) -> None:
        if not math.isfinite(value) or value < 0:
            return
        self.total += 1
        self.sum_value += value
        for index, bound in enumerate(_BUCKETS):
            if value <= bound:
                self.counts[index] += 1


@dataclass
class ChatContext:
    model: str
    upstream: str
    stream: bool
    started: float = field(default_factory=time.perf_counter)
    first_byte_at: float | None = None
    done: bool = False


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**labels: str) -> str:
    if not labels:
        return ""
    parts = [f'{key}="{_escape(value)}"' for key, value in labels.items()]
    return "{" + ",".join(parts) + "}"


def begin_chat(model: str, upstream: str, stream: bool) -> ChatContext:
    return ChatContext(model=model or "", upstream=upstream or "local", stream=stream)


def _finish_histogram(store, key, value: float) -> None:
    with _lock:
        histogram = store.get(key)
        if histogram is None:
            histogram = Histogram()
            store[key] = histogram
        histogram.observe(value)


def finish_chat(ctx: ChatContext, status: int) -> None:
    """记录一次 chat 请求的整体耗时；非流式同时视为首字节耗时。"""
    try:
        if ctx.done:
            return
        now = time.perf_counter()
        duration = max(0.0, now - ctx.started)
        stream_label = "true" if ctx.stream else "false"
        key = (ctx.model, ctx.upstream, stream_label)
        _finish_histogram(_duration, key, duration)
        _finish_histogram(_first_byte, key, ctx.first_byte_at or duration)
        with _lock:
            counter_key = (ctx.model, ctx.upstream, str(status))
            _model_requests[counter_key] = _model_requests.get(counter_key, 0) + 1
        ctx.done = True
    except Exception:  # noqa: BLE001 - 指标永远不能影响业务
        pass


def mark_first_byte(ctx: ChatContext) -> None:
    try:
        if ctx.first_byte_at is None:
            ctx.first_byte_at = time.perf_counter()
    except Exception:  # noqa: BLE001
        pass


def wrap_response(response: Any, ctx: ChatContext) -> Any:
    """非流式在此收尾；流式包一层 body 迭代器，统计首字节与整体耗时。"""
    try:
        iterator = getattr(response, "body_iterator", None)
        if iterator is None:
            finish_chat(ctx, int(getattr(response, "status_code", 200) or 200))
            return response

        async def wrapped():
            try:
                async for chunk in iterator:
                    mark_first_byte(ctx)
                    yield chunk
            finally:
                finish_chat(ctx, int(getattr(response, "status_code", 200) or 200))

        response.body_iterator = wrapped()
    except Exception:  # noqa: BLE001
        pass
    return response


def observe_http(endpoint: str, method: str, status: int) -> None:
    try:
        with _lock:
            key = (endpoint or "", method or "", str(status))
            _requests[key] = _requests.get(key, 0) + 1
    except Exception:  # noqa: BLE001
        pass


def inflight_add(delta: int) -> None:
    global _inflight
    try:
        with _lock:
            _inflight = max(0, _inflight + delta)
    except Exception:  # noqa: BLE001
        pass


def discovery_result(upstream: str, ok: bool) -> None:
    try:
        with _lock:
            key = (upstream, "success" if ok else "failure")
            _discovery[key] = _discovery.get(key, 0) + 1
            if ok:
                _last_success[upstream] = time.time()
    except Exception:  # noqa: BLE001
        pass


def fallback_result(requested: str, served_by: str | None) -> None:
    try:
        with _lock:
            if served_by is None:
                _exhausted[requested] = _exhausted.get(requested, 0) + 1
            else:
                key = (requested, served_by)
                _fallbacks[key] = _fallbacks.get(key, 0) + 1
    except Exception:  # noqa: BLE001
        pass


def _render_histogram(name: str, store: dict[tuple[str, str, str], Histogram]) -> list[str]:
    lines: list[str] = []
    with _lock:
        snapshot = list(store.items())
    for (model, upstream, stream), histogram in snapshot:
        labels = {"model": model, "upstream": upstream, "stream": stream}
        cumulative = 0
        for bound, count in zip(_BUCKETS, histogram.counts):
            cumulative = count
            lines.append(
                f"{name}_bucket{_labels(**labels, le=str(bound))} {cumulative}"
            )
        lines.append(f"{name}_bucket{_labels(**labels, le='+Inf')} {histogram.total}")
        lines.append(f"{name}_sum{_labels(**labels)} {histogram.sum_value}")
        lines.append(f"{name}_count{_labels(**labels)} {histogram.total}")
    return lines


def render(dynamic: dict[str, float] | None = None) -> str:
    """渲染 Prometheus 文本；任何异常都返回最小可用输出。"""
    try:
        lines = [
            "# HELP quasheng_http_requests_total HTTP 请求总数（按端点/方法/状态码）。",
            "# TYPE quasheng_http_requests_total counter",
        ]
        with _lock:
            requests = list(_requests.items())
            model_requests = list(_model_requests.items())
            discovery = list(_discovery.items())
            last_success = dict(_last_success)
            fallbacks = list(_fallbacks.items())
            exhausted = dict(_exhausted)
            inflight = _inflight
        for (endpoint, method, status), count in requests:
            lines.append(
                f"quasheng_http_requests_total{_labels(endpoint=endpoint, method=method, status=status)} {count}"
            )

        lines += [
            "# HELP quasheng_model_requests_total 模型请求总数（按请求模型/上游/状态码）。",
            "# TYPE quasheng_model_requests_total counter",
        ]
        for (model, upstream, status), count in model_requests:
            lines.append(
                f"quasheng_model_requests_total{_labels(model=model, upstream=upstream, status=status)} {count}"
            )

        lines += [
            "# HELP quasheng_request_duration_seconds 模型请求整体耗时。",
            "# TYPE quasheng_request_duration_seconds histogram",
        ]
        lines += _render_histogram("quasheng_request_duration_seconds", _duration)
        lines += [
            "# HELP quasheng_first_byte_seconds 模型请求首字节耗时（非流式等于整体耗时）。",
            "# TYPE quasheng_first_byte_seconds histogram",
        ]
        lines += _render_histogram("quasheng_first_byte_seconds", _first_byte)

        lines += [
            "# HELP quasheng_discovery_total 上游模型发现次数。",
            "# TYPE quasheng_discovery_total counter",
        ]
        for (upstream, result), count in discovery:
            lines.append(
                f"quasheng_discovery_total{_labels(upstream=upstream, result=result)} {count}"
            )
        lines += [
            "# HELP quasheng_discovery_last_success_timestamp 上游最近一次发现成功时间。",
            "# TYPE quasheng_discovery_last_success_timestamp gauge",
        ]
        for upstream, timestamp in last_success.items():
            lines.append(
                f"quasheng_discovery_last_success_timestamp{_labels(upstream=upstream)} {timestamp}"
            )

        lines += [
            "# HELP quasheng_fallback_total 回退成功次数（按请求模型与实际来源）。",
            "# TYPE quasheng_fallback_total counter",
        ]
        for (requested, served), count in fallbacks:
            lines.append(
                f"quasheng_fallback_total{_labels(requested=requested, served_by=served)} {count}"
            )
        lines += [
            "# HELP quasheng_fallback_exhausted_total 回退链条耗尽次数。",
            "# TYPE quasheng_fallback_exhausted_total counter",
        ]
        for requested, count in exhausted.items():
            lines.append(
                f"quasheng_fallback_exhausted_total{_labels(requested=requested)} {count}"
            )

        gauges: dict[str, float] = {
            "quasheng_inflight_requests": float(inflight),
        }
        for key, value in (dynamic or {}).items():
            gauges[key] = value
        lines += [
            "# HELP quasheng_inflight_requests 当前在途 HTTP 请求数。",
            "# TYPE quasheng_inflight_requests gauge",
        ]
        for key, value in gauges.items():
            if key.startswith("quasheng_inflight_requests"):
                lines.append(f"{key} {value}")

        lines += [
            "# HELP quasheng_gate_active 并发闸门当前在途数。",
            "# TYPE quasheng_gate_active gauge",
            "# HELP quasheng_gate_queue_len 并发闸门当前排队数。",
            "# TYPE quasheng_gate_queue_len gauge",
            "# HELP quasheng_upstream_up 上游可用状态（1=ready，0=其他）。",
            "# TYPE quasheng_upstream_up gauge",
        ]
        for key, value in gauges.items():
            if key.startswith(("quasheng_gate_", "quasheng_upstream_up")):
                lines.append(f"{key} {value}")
        return "\n".join(lines) + "\n"
    except Exception:  # noqa: BLE001 - /metrics 渲染失败不能影响任何东西
        return "# metrics render failed\n"


def reset() -> None:
    """清空指标（测试用）。"""
    global _inflight
    with _lock:
        _requests.clear()
        _model_requests.clear()
        _duration.clear()
        _first_byte.clear()
        _discovery.clear()
        _last_success.clear()
        _fallbacks.clear()
        _exhausted.clear()
        _inflight = 0
