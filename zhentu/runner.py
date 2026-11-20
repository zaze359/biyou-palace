"""任务执行器：状态机、顺序 DAG、事件、取消与聚合。"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from .aggregator import Aggregator, ConcatAggregator
from .ledger import TaskStore
from .orchestration import (
    Executor,
    Planner,
    single_step_plan,
    validate_plan,
)


class TaskRunner:
    def __init__(
        self,
        *,
        ledger: TaskStore,
        planner: Planner,
        executor: Executor,
        router: Callable[[dict[str, Any]], dict[str, Any] | None],
        clock: Callable[[], int],
        aggregator: Aggregator | None = None,
        max_events: int | None = None,
        retention_ms: int | None = None,
        step_timeout_s: float = 120.0,
        aggregator_timeout_s: float = 30.0,
    ) -> None:
        self._ledger = ledger
        self._planner = planner
        self._executor = executor
        self._router = router
        self._clock = clock
        self._aggregator = aggregator or ConcatAggregator()
        self._max_events = max_events
        self._retention_ms = retention_ms
        self._step_timeout_s = step_timeout_s
        self._aggregator_timeout_s = aggregator_timeout_s

    async def run(self, task_id: str) -> None:
        task = self._ledger.get_task(task_id)
        if task is None or task["status"] != "queued":
            return
        if task["cancel_requested"]:
            self._finish_canceled(task, reason="user_canceled", steps=[])
            return

        self._ledger.update_task(
            task_id, now_ms=self._clock(), status="running", started_at_ms=self._clock()
        )
        request = task["request_json"]
        try:
            plan = validate_plan(await self._planner.plan(request))
        except Exception:
            plan = validate_plan(single_step_plan(request))

        self._ledger.update_task(
            task_id,
            now_ms=self._clock(),
            plan_json=plan,
            planner_model=self._planner.__class__.__name__,
        )
        self._event(
            task_id,
            "task/planned",
            {"plan_id": plan.get("plan_id"), "steps": len(plan["steps"])},
        )
        for step in plan["steps"]:
            self._ledger.upsert_step(task_id, step, status="queued", now_ms=self._clock())

        results: dict[str, dict[str, Any]] = {}
        sources: list[dict[str, Any]] = []
        partials: list[dict[str, Any]] = []
        contents: list[tuple[str, str]] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        required_failed = False
        canceled = False
        deadline_hit = False

        steps = plan["steps"]
        for index, step in enumerate(steps):
            current = self._ledger.get_task(task_id) or task
            if current["cancel_requested"]:
                canceled = True
                self._cancel_remaining(task_id, steps[index:])
                break
            remaining_s = (int(current["deadline_at_ms"]) - self._clock()) / 1000
            if remaining_s <= 0:
                deadline_hit = True
                self._cancel_remaining(task_id, steps[index:], reason="deadline")
                break

            failed_dep = next(
                (
                    dep
                    for dep in step.get("depends_on") or []
                    if results.get(dep, {}).get("status") != "ok"
                ),
                None,
            )
            if failed_dep is not None:
                results[step["id"]] = {
                    "status": "failed",
                    "error_code": "dependency_failed",
                }
                self._fail_step(
                    task_id, step, "dependency_failed", f"依赖步骤未成功：{failed_dep}"
                )
                if step.get("required", True):
                    required_failed = True
                else:
                    partials.append(
                        {"step_id": step["id"], "reason": "dependency_failed", "output_ref": None}
                    )
                continue

            route = self._router(step)
            decision_id = None
            if route and route.get("selected"):
                decision_id = self._ledger.record_route_decision(
                    task_id=task_id,
                    step_id=step["id"],
                    request=route.get("request") or {},
                    selected=route["selected"],
                    rejected=route.get("rejected") or [],
                    now_ms=self._clock(),
                )
            else:
                results[step["id"]] = {
                    "status": "failed",
                    "error_code": "no_candidate",
                }
                self._fail_step(task_id, step, "no_candidate", "没有可路由的节点")
                if step.get("required", True):
                    required_failed = True
                else:
                    partials.append(
                        {"step_id": step["id"], "reason": "no_candidate", "output_ref": None}
                    )
                continue

            selected = route["selected"]
            self._ledger.update_step(
                task_id, step["id"], status="running", now_ms=self._clock()
            )
            self._event(
                task_id,
                "step/started",
                {
                    "step_id": step["id"],
                    "node_id": selected.get("node_id"),
                    "model": selected.get("model"),
                    "route_decision_id": decision_id,
                },
            )

            timeout_s = min(self._step_timeout_s, remaining_s)
            try:
                outcome = await asyncio.wait_for(
                    self._executor.execute(step, selected, timeout_s=timeout_s),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                outcome = {
                    "status": "failed",
                    "content": None,
                    "output_ref": None,
                    "usage": None,
                    "latency_ms": int(timeout_s * 1000),
                    "error_code": "step_timeout",
                }
            except Exception as exc:
                outcome = {
                    "status": "failed",
                    "content": None,
                    "output_ref": None,
                    "usage": None,
                    "latency_ms": 0,
                    "error_code": "executor_error",
                    "error_message": str(exc),
                }

            if outcome.get("status") == "ok":
                results[step["id"]] = {"status": "ok"}
                self._ledger.update_step(
                    task_id,
                    step["id"],
                    status="ok",
                    now_ms=self._clock(),
                    output_ref=outcome.get("output_ref"),
                )
                source = {
                    "step_id": step["id"],
                    "node_id": selected.get("node_id"),
                    "model": selected.get("model"),
                    "route_decision_id": decision_id,
                    "output_ref": outcome.get("output_ref"),
                }
                sources.append(source)
                content = outcome.get("content")
                if content:
                    contents.append((step["id"], content))
                step_usage = outcome.get("usage") or {}
                usage["prompt_tokens"] += int(step_usage.get("prompt_tokens") or 0)
                usage["completion_tokens"] += int(
                    step_usage.get("completion_tokens") or 0
                )
                self._event(
                    task_id,
                    "step/result",
                    {
                        "step_id": step["id"],
                        "node_id": selected.get("node_id"),
                        "model": selected.get("model"),
                        "route_decision_id": decision_id,
                        "status": "ok",
                        "output_ref": outcome.get("output_ref"),
                    },
                )
            else:
                code = str(outcome.get("error_code") or "step_failed")
                results[step["id"]] = {"status": "failed", "error_code": code}
                self._ledger.update_step(
                    task_id,
                    step["id"],
                    status="failed",
                    now_ms=self._clock(),
                    error_code=code,
                )
                self._event(
                    task_id,
                    "step/failed",
                    {
                        "step_id": step["id"],
                        "status": "failed",
                        "error_code": code,
                        "message": outcome.get("error_message"),
                    },
                )
                if step.get("required", True):
                    required_failed = True
                else:
                    partials.append(
                        {
                            "step_id": step["id"],
                            "reason": code,
                            "output_ref": outcome.get("output_ref"),
                        }
                    )

            current = self._ledger.get_task(task_id) or task
            if current["cancel_requested"]:
                canceled = True
                self._cancel_remaining(task_id, steps[index + 1 :])
                break

        if canceled:
            self._finish_canceled(task, reason="user_canceled")
            return
        if required_failed:
            self._finish(
                task_id,
                status="failed",
                terminal_reason="required_step_failed",
                result=self._result(
                    task_id,
                    status="failed",
                    mode="cluster",
                    answer=None,
                    sources=sources,
                    partials=partials,
                    usage=usage,
                    error=_error("required_step_failed", "必需步骤失败"),
                    terminal_reason="required_step_failed",
                ),
                error={"code": "required_step_failed", "message": "必需步骤失败"},
            )
            return

        self._ledger.update_task(task_id, now_ms=self._clock(), status="aggregating")
        self._event(task_id, "aggregate/started", {})
        ordered = [
            content
            for step_id in plan["aggregate"].get("input_step_ids") or []
            for found_id, content in contents
            if found_id == step_id
        ]
        try:
            answer = await self._aggregator.aggregate(
                ordered,
                instructions=plan["aggregate"].get("instructions"),
                timeout_s=self._aggregator_timeout_s,
            )
        except Exception:
            answer = "\n\n".join(ordered) if ordered else None
        status = "partial" if (partials or deadline_hit) else "completed"
        terminal_reason = "deadline" if deadline_hit else None
        self._event(
            task_id,
            "aggregate/result",
            {
                "status": status,
                "sources": len(sources),
                "partials": len(partials),
                "aggregator": type(self._aggregator).__name__,
            },
        )
        result = self._result(
            task_id,
            status=status,
            mode="cluster",
            answer=answer,
            sources=sources,
            partials=partials,
            usage=usage,
            error=None,
            terminal_reason=terminal_reason,
        )
        self._finish(
            task_id,
            status=status,
            terminal_reason=terminal_reason,
            result=result,
        )

    # ---------------------------------------------------------------- 辅助

    def _cancel_remaining(
        self,
        task_id: str,
        steps: list[dict[str, Any]],
        *,
        reason: str = "canceled",
    ) -> None:
        for step in steps:
            self._ledger.update_step(
                task_id, step["id"], status="canceled", now_ms=self._clock()
            )
            self._event(
                task_id,
                "step/canceled",
                {"step_id": step["id"], "reason": reason},
            )

    def _fail_step(
        self,
        task_id: str,
        step: dict[str, Any],
        code: str,
        message: str,
    ) -> None:
        self._ledger.upsert_step(task_id, step, status="failed", now_ms=self._clock())
        self._ledger.update_step(
            task_id, step["id"], status="failed", now_ms=self._clock(), error_code=code
        )
        self._event(
            task_id,
            "step/failed",
            {"step_id": step["id"], "status": "failed", "error_code": code, "message": message},
        )

    def _finish_canceled(
        self, task: dict[str, Any], *, reason: str, steps: list[dict[str, Any]] | None = None
    ) -> None:
        task_id = task["task_id"]
        self._cancel_remaining(task_id, steps or [], reason=reason)
        result = self._result(
            task_id,
            status="canceled",
            mode="cluster",
            answer=None,
            sources=[],
            partials=[],
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            error=None,
            terminal_reason=reason,
        )
        self._finish(
            task_id, status="canceled", terminal_reason=reason, result=result
        )
        self._event(task_id, "task/canceled", {"reason": reason})

    def _finish(
        self,
        task_id: str,
        *,
        status: str,
        terminal_reason: str | None,
        result: dict[str, Any],
        error: dict[str, Any] | None = None,
    ) -> None:
        now_ms = self._clock()
        self._ledger.update_task(
            task_id,
            now_ms=now_ms,
            status=status,
            terminal_reason=terminal_reason,
            result_json=result,
            error_json=error,
            finished_at_ms=now_ms,
        )
        event_type = {
            "completed": "task/completed",
            "partial": "task/completed",
            "failed": "task/failed",
            "canceled": "task/canceled",
        }[status]
        self._event(task_id, event_type, {"status": status})

    def _result(
        self,
        task_id: str,
        *,
        status: str,
        mode: str,
        answer: str | None,
        sources: list[dict[str, Any]],
        partials: list[dict[str, Any]],
        usage: dict[str, Any],
        error: dict[str, Any] | None,
        terminal_reason: str | None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "task_id": task_id,
            "status": status,
            "terminal_reason": terminal_reason,
            "mode": mode,
            "answer": answer,
            "sources": sources,
            "partials": partials,
            "usage": {
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "steps": len(sources),
            },
            "error": error,
        }

    def _event(self, task_id: str, event_type: str, data: dict[str, Any]) -> None:
        self._ledger.append_event(
            task_id,
            event_type,
            data,
            now_ms=self._clock(),
            max_events=self._max_events,
            retention_ms=self._retention_ms,
        )


def _error(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message, "type": "orchestration_error", "param": None}
