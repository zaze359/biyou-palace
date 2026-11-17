"""阵图选路指标（进程内、无第三方依赖）。

只统计 `/route` 决策本身，不记录 node_id / model / endpoint 标签：
避免高基数，也避免把拓扑信息暴露给监控系统。进程重启后归零。
"""
from __future__ import annotations

import threading
from collections import Counter
from typing import Any, Iterable, Mapping

DEFAULT_BUCKETS = (0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)


class RouteMetrics:
    def __init__(self, buckets: Iterable[float] = DEFAULT_BUCKETS) -> None:
        self._buckets = tuple(sorted(float(bound) for bound in buckets))
        self._lock = threading.Lock()
        self._total: Counter[tuple[str, str]] = Counter()
        self._rejected: Counter[str] = Counter()
        self._bucket_counts = [0] * len(self._buckets)
        self._sum = 0.0
        self._count = 0

    def observe(
        self,
        *,
        strategy: str,
        selected: bool,
        rejected: Iterable[Mapping[str, Any]],
        duration_s: float,
    ) -> None:
        result = "selected" if selected else "no_route"
        with self._lock:
            self._total[(strategy, result)] += 1
            for item in rejected:
                reason = str(item.get("reason") or "unknown")
                self._rejected[reason] += 1
            for index, bound in enumerate(self._buckets):
                if duration_s <= bound:
                    self._bucket_counts[index] += 1
            self._sum += duration_s
            self._count += 1

    def render(self) -> str:
        with self._lock:
            total = sorted(self._total.items())
            rejected = sorted(self._rejected.items())
            buckets = list(zip(self._buckets, self._bucket_counts))
            count = self._count
            total_seconds = self._sum

        lines = [
            "# HELP zhentu_route_total 路由决策总数。",
            "# TYPE zhentu_route_total counter",
        ]
        for (strategy, result), value in total:
            lines.append(
                f'zhentu_route_total{{strategy="{_escape(strategy)}",'
                f'result="{_escape(result)}"}} {value}'
            )
        lines.extend(
            [
                "# HELP zhentu_route_rejected_total 被拒绝候选数。",
                "# TYPE zhentu_route_rejected_total counter",
            ]
        )
        for reason, value in rejected:
            lines.append(
                f'zhentu_route_rejected_total{{reason="{_escape(reason)}"}} {value}'
            )
        lines.extend(
            [
                "# HELP zhentu_route_duration_seconds 路由决策耗时。",
                "# TYPE zhentu_route_duration_seconds histogram",
            ]
        )
        for bound, value in buckets:
            lines.append(
                f'zhentu_route_duration_seconds_bucket{{le="{_format_bound(bound)}"}} {value}'
            )
        lines.append(f'zhentu_route_duration_seconds_bucket{{le="+Inf"}} {count}')
        lines.append(f"zhentu_route_duration_seconds_sum {total_seconds:.6f}")
        lines.append(f"zhentu_route_duration_seconds_count {count}")
        return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_bound(value: float) -> str:
    text = f"{value:g}"
    return text
