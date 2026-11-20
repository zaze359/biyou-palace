"""编排协议的执行侧：计划生成、校验与 NewAPI 调用。

只依赖标准库 + 现有契约；不引入新的 HTTP 客户端依赖。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
import urllib.error
import urllib.request
from typing import Any, Protocol

from .config import Secret
from .models import CANONICAL_CAPABILITIES, normalize_capability

FAILURE_POLICIES = {"fail_task", "partial"}
MAX_STEPS = 12
MAX_PARALLEL = 8


class PlanError(ValueError):
    """计划不合法，调用方应回退单步。"""


class Planner(Protocol):
    async def plan(self, request: dict[str, Any]) -> dict[str, Any]: ...


class Executor(Protocol):
    async def execute(
        self,
        step: dict[str, Any],
        route: dict[str, Any],
        *,
        timeout_s: float,
    ) -> dict[str, Any]: ...


def single_step_plan(request: dict[str, Any]) -> dict[str, Any]:
    """默认降级计划：一次 chat，直接聚合。"""
    capabilities = request.get("requirements", {}).get("capabilities") or ["chat"]
    capability = "chat" if "chat" in capabilities else capabilities[0]
    messages = request.get("input", {}).get("messages") or []
    return {
        "plan_id": "plan-" + uuid.uuid4().hex,
        "intent": None,
        "max_steps": 1,
        "max_parallel": 1,
        "steps": [
            {
                "id": "s1",
                "goal": "回答用户请求",
                "capability": capability,
                "input": {"messages": messages},
                "depends_on": [],
                "parallel_group": None,
                "exclusive": False,
                "required": True,
                "failure_policy": "fail_task",
                "model_hint": None,
                "input_ref": None,
                "output_schema": None,
            }
        ],
        "aggregate": {
            "mode": "concat",
            "input_step_ids": ["s1"],
            "include_partial": True,
            "instructions": None,
        },
    }


def validate_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise PlanError("plan 必须是 JSON 对象")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PlanError("plan.steps 不能为空")
    max_steps = plan.get("max_steps", len(steps))
    max_parallel = plan.get("max_parallel", 1)
    if not isinstance(max_steps, int) or max_steps < len(steps) or max_steps > MAX_STEPS:
        raise PlanError(f"plan.max_steps 必须在 {len(steps)}..{MAX_STEPS}")
    if not isinstance(max_parallel, int) or not 1 <= max_parallel <= MAX_PARALLEL:
        raise PlanError(f"plan.max_parallel 必须在 1..{MAX_PARALLEL}")

    seen: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            raise PlanError("plan.steps[] 必须是对象")
        step_id = step.get("id")
        if not isinstance(step_id, str) or not step_id:
            raise PlanError("plan.steps[].id 不能为空")
        if step_id in seen:
            raise PlanError(f"step id 重复：{step_id}")
        try:
            capability = normalize_capability(step.get("capability") or "chat")
        except ValueError as exc:
            raise PlanError(str(exc)) from None
        if capability not in CANONICAL_CAPABILITIES:
            raise PlanError(f"step {step_id} 能力不受支持：{capability}")
        step["capability"] = capability

        depends_on = step.get("depends_on") or []
        if not isinstance(depends_on, list):
            raise PlanError(f"step {step_id} 的 depends_on 必须是数组")
        for dep in depends_on:
            if dep not in seen:
                raise PlanError(f"step {step_id} 依赖了未声明的前序步骤：{dep}")
        if step.get("failure_policy", "fail_task") not in FAILURE_POLICIES:
            raise PlanError(f"step {step_id} 的 failure_policy 非法")
        if not isinstance(step.get("input"), dict):
            raise PlanError(f"step {step_id} 缺少 input")
        seen.add(step_id)

    aggregate = plan.get("aggregate")
    if not isinstance(aggregate, dict):
        raise PlanError("plan.aggregate 必须是对象")
    input_ids = aggregate.get("input_step_ids")
    if not isinstance(input_ids, list) or not input_ids:
        raise PlanError("aggregate.input_step_ids 不能为空")
    if any(step_id not in seen for step_id in input_ids):
        raise PlanError("aggregate.input_step_ids 引用了不存在的步骤")
    plan["max_steps"] = max_steps
    plan["max_parallel"] = max_parallel
    return plan


def _tag_plan(plan: dict[str, Any], source: str, detail: str | None = None) -> dict[str, Any]:
    """给计划打来源标记。

    runner 把标记写进 `task/planned` 事件与账本，运维据此能回答
    「这次为什么只有一步 / 为什么没有拆」——没有标记时只能翻代码。
    """
    marked = dict(plan)
    marked["planner_source"] = source
    if detail:
        marked["planner_note"] = detail
    return marked


class SingleStepPlanner:
    """无规划模型时的默认计划器。"""

    async def plan(self, request: dict[str, Any]) -> dict[str, Any]:
        return _tag_plan(single_step_plan(request), "single_step", "未配置规划模型，单步执行")


def _drop_unavailable_steps(plan: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """把能力在快照里没有可用节点的步骤摘掉。

    只处理「能力完全无人可用」这一种确定情况；有节点但被拒绝（负载/租约）留给运行时选路，
    那里有更准确的拒绝原因。若摘完没有步骤了，保留原计划由运行时报 no_candidate，
    这样错误码仍然可读。
    """
    available = snapshot.get("available") or {}
    if not isinstance(available, dict) or not available:
        return plan
    steps = [s for s in plan.get("steps") or [] if s.get("capability") in available]
    if not steps or len(steps) == len(plan.get("steps") or []):
        return plan
    dropped = [
        s.get("id") for s in plan.get("steps") or [] if s.get("capability") not in available
    ]
    kept_ids = {s.get("id") for s in steps}
    for step in steps:
        deps = [d for d in (step.get("depends_on") or []) if d in kept_ids]
        step["depends_on"] = deps
    aggregate = dict(plan.get("aggregate") or {})
    aggregate["input_step_ids"] = [
        sid for sid in (aggregate.get("input_step_ids") or []) if sid in kept_ids
    ]
    updated = dict(plan)
    updated["steps"] = steps
    updated["aggregate"] = aggregate
    updated["dropped_steps"] = dropped
    return updated


class PlannerClient:
    """优先调用规划模型；失败、超时或非法 JSON 时回退单步。"""

    def __init__(
        self,
        *,
        base_url: str,
        token: Secret | None,
        model: str = "",
        timeout_s: float = 30.0,
        fallback: Planner | None = None,
        max_retries: int = 1,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._model = model
        self._timeout_s = timeout_s
        self._fallback = fallback or SingleStepPlanner()
        # 校验失败后把错误回喂重试的次数（0 表示不重试）；上限 3 防止弱模型无限打转
        self._max_retries = max(0, min(int(max_retries), 3))

    async def plan(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._base_url or not self._model or not self._token:
            return await self._fallback_plan(
                request, "planner_not_configured", "未配置规划模型或凭据（ZHENTU_PLANNER_MODEL）"
            )
        snapshot = request.get("capability_snapshot") or {}
        # 弱模型（0.5B 级）首次几乎必然产出非法 JSON。把校验错误回喂再试，
        # 对上位模型只是多一次机会，对弱模型才是能不能用的差别。
        last_error: str | None = None
        attempts = max(1, self._max_retries + 1)
        for attempt in range(attempts):
            try:
                raw = await asyncio.to_thread(
                    self._request_plan, request, snapshot, last_error
                )
            except urllib.error.HTTPError as exc:
                # 服务端错误重试没意义（鉴权/限流/额度），直接回退并说明原因
                return await self._fallback_plan(
                    request, "planner_error", f"HTTPError {exc.code}"
                )
            except json.JSONDecodeError as exc:
                # 模型输出的不是 JSON：这是**可纠错**的格式问题，交给下一轮重试
                last_error = f"输出不是合法 JSON：{exc}"[:200]
                continue
            except Exception as exc:  # 网络/超时
                return await self._fallback_plan(
                    request, "planner_error", f"{type(exc).__name__}: {exc}"[:200]
                )
            try:
                plan = validate_plan(raw)
            except PlanError as exc:
                last_error = str(exc)
                continue
            except json.JSONDecodeError as exc:
                last_error = f"输出不是合法 JSON：{exc}"
                continue
            note = None if attempt == 0 else f"第 {attempt + 1} 次尝试成功"
            # 规划器可能返回无人可用的能力：在计划阶段就把这类步骤剔掉，
            # 否则要等执行时选路失败，任务白跑一轮。
            return _tag_plan(_drop_unavailable_steps(plan, snapshot), "planner", note)
        return await self._fallback_plan(
            request,
            "planner_invalid",
            f"{attempts} 次尝试均未通过校验，最后一次错误：{last_error}"[:300],
        )

    async def _fallback_plan(
        self, request: dict[str, Any], reason: str, detail: str
    ) -> dict[str, Any]:
        plan = await self._fallback.plan(request)
        plan = dict(plan)
        plan["planner_source"] = "fallback"
        plan["planner_note"] = f"{reason}：{detail}"[:300]
        return plan

    def _request_plan(
        self,
        request: dict[str, Any],
        snapshot: dict[str, Any],
        last_error: str | None = None,
    ) -> dict[str, Any]:
        messages = request.get("input", {}).get("messages") or []
        available = snapshot.get("available") or []
        capability_hint = (
            "当前可用能力快照（只能使用这些；没有可用节点的能力不要写进计划）："
            + json.dumps(available, ensure_ascii=False)
            if available
            else "当前没有可用的能力快照，请保守地只做单步 chat 计划。"
        )
        prompt = (
            "你是阵图编排器。只输出一个 JSON 对象，不要解释、不要 markdown 代码块。\n"
            "硬性要求（违反即作废）：\n"
            "1. 所有键名必须用双引号；字符串值必须用双引号；不允许注释、不允许尾逗号。\n"
            "2. steps[].id 必须是字符串（如 \"s1\"）；max_steps 必须 >= 实际步骤数；max_parallel 在 1..8。\n"
            "3. failure_policy 只能是 \"fail_task\" 或 \"partial\"；required 必须是 true/false。\n"
            "4. aggregate.mode 只能是 \"concat\" 或 \"llm\"；input_step_ids 必须引用存在的 step id。\n"
            "5. capability 只能取自 " + json.dumps(list(CANONICAL_CAPABILITIES), ensure_ascii=False) + "。\n"
            "6. 单步即可回答时就用 1 步，不要为了凑数拆分。\n"
            "结构：{\"plan_id\":string, \"intent\":string, \"max_steps\":int, \"max_parallel\":int, "
            "\"steps\":[{\"id\":string, \"goal\":string, \"capability\":string, "
            "\"input\":{\"messages\":[{\"role\":\"user\",\"content\":string}]}, "
            "\"depends_on\":[string], \"required\":bool, \"failure_policy\":string}], "
            "\"aggregate\":{\"mode\":string, \"input_step_ids\":[string]}}。\n"
            f"最多 {MAX_STEPS} 步。{capability_hint}"
        )
        if last_error:
            prompt += (
                "\n\n上一次输出未通过校验，错误是：" + last_error[:200]
                + "\n请严格按上述格式重新输出，只给 JSON。"
            )
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(messages, ensure_ascii=False)},
            ],
            "stream": False,
            "temperature": 0,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token.get()}"
        req = urllib.request.Request(
            chat_url(self._base_url),
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout_s) as response:
            body = json.loads(response.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        return json.loads(content)


class NewApiExecutor:
    """经 NewAPI 的 OpenAI 兼容端点执行单个 chat 步骤。"""

    def __init__(
        self,
        *,
        base_url: str,
        token: Secret | None,
        timeout_s: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_s = timeout_s

    async def execute(
        self,
        step: dict[str, Any],
        route: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        if step.get("capability") != "chat":
            return _failed("unsupported_capability", "P2 仅支持 chat 执行")
        if not self._base_url or not self._token:
            return _failed("execution_unavailable", "未配置 ZHENTU_NEWAPI_BASE_URL/API_KEY")
        messages = (step.get("input") or {}).get("messages") or []
        payload = {
            "model": route.get("model") or "",
            "messages": messages,
            "stream": False,
        }
        started = time.perf_counter()
        try:
            result = await asyncio.to_thread(
                self._post_chat, payload, timeout_s or self._timeout_s
            )
        except urllib.error.HTTPError as exc:
            return _failed(f"upstream_http_{exc.code}", f"NewAPI 返回 {exc.code}")
        except Exception as exc:  # 网络/解析失败
            return _failed("upstream_error", str(exc))
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return _failed("invalid_upstream_response", "NewAPI 响应缺少 choices[0]")
        usage = result.get("usage") or {}
        return {
            "status": "ok",
            "content": content,
            "output_ref": None,
            "usage": {
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
            },
            "latency_ms": latency_ms,
            "error_code": None,
        }

    def _post_chat(self, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token.get()}",
        }
        req = urllib.request.Request(
            chat_url(self._base_url), data=data, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))


def chat_url(base_url: str) -> str:
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        return root + "/chat/completions"
    return root + "/v1/chat/completions"


def _failed(code: str, message: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "content": None,
        "output_ref": None,
        "usage": None,
        "latency_ms": 0,
        "error_code": code,
        "error_message": message,
    }
