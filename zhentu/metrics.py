"""阵图指标（进程内、无第三方依赖）。

`RouteMetrics` 只统计 `/route` 决策本身；任务指标在 scrape 时由账本现状渲染。
两者都不记录 node_id / model / endpoint / prompt：避免高基数，也避免把拓扑
信息暴露给监控系统。
"""
from __future__ import annotations

import threading
from collections import Counter
from typing import Any, Iterable, Mapping

DEFAULT_BUCKETS = (0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)
TERMINAL_TASK_STATUSES = ("completed", "partial", "failed", "canceled")


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


def render_task_metrics(
    status_counts: Mapping[str, int],
    *,
    active: int,
) -> str:
    """按账本现状渲染任务指标。

    `status_counts` 是 `SELECT status, COUNT(*) ... GROUP BY status` 的结果；
    created_total 取所有状态之和，terminal_total 固定输出四个终态（含 0）。
    """
    total = sum(int(value) for value in status_counts.values())
    lines = [
        "# HELP zhentu_tasks_created_total 账本中创建过的任务总数。",
        "# TYPE zhentu_tasks_created_total counter",
        f"zhentu_tasks_created_total {total}",
        "# HELP zhentu_tasks_terminal_total 按终态统计的任务数。",
        "# TYPE zhentu_tasks_terminal_total counter",
    ]
    for status in TERMINAL_TASK_STATUSES:
        value = int(status_counts.get(status, 0))
        lines.append(
            f'zhentu_tasks_terminal_total{{status="{status}"}} {value}'
        )
    lines.extend(
        [
            "# HELP zhentu_tasks_active 活跃任务数（queued/running/aggregating）。",
            "# TYPE zhentu_tasks_active gauge",
            f"zhentu_tasks_active {int(active)}",
        ]
    )
    return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_bound(value: float) -> str:
    text = f"{value:g}"
    return text
