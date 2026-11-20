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


class SingleStepPlanner:
    """无规划模型时的默认计划器。"""

    async def plan(self, request: dict[str, Any]) -> dict[str, Any]:
        return single_step_plan(request)


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
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._model = model
        self._timeout_s = timeout_s
        self._fallback = fallback or SingleStepPlanner()

    async def plan(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._base_url or not self._model or not self._token:
            return await self._fallback.plan(request)
        try:
            raw = await asyncio.to_thread(self._request_plan, request)
            return validate_plan(raw)
        except Exception:
            return await self._fallback.plan(request)

    def _request_plan(self, request: dict[str, Any]) -> dict[str, Any]:
        messages = request.get("input", {}).get("messages") or []
        prompt = (
            "你是阵图编排器。只输出 JSON 计划，不要解释。"
            "计划结构：{plan_id, intent, max_steps, max_parallel, steps:["
            "{id, goal, capability, input:{messages}, depends_on, parallel_group,"
            " exclusive, required, failure_policy, model_hint, input_ref, output_schema}],"
            " aggregate:{mode, input_step_ids, include_partial, instructions}}。"
            f"最多 {MAX_STEPS} 步；capability 只能是 {list(CANONICAL_CAPABILITIES)}。"
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
