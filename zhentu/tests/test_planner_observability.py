"""编排链路的计划来源标记、能力快照与取消语义测试。

对应 `docs/reports/daojian-orchestration-2026-11-21.md` 第四节的三处发现：
1. 默认配置不拆步且不可观测 → `task/planned` 必须带 `planner_source`
2. 取消是批次边界语义 → `task/canceled` 必须说明作用域
3. 规划器拿不到能力快照 → 快照注入 + 无人可用能力在计划阶段剔除
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from zhentu import orchestration
from zhentu.orchestration import (
    PlannerClient,
    SingleStepPlanner,
    _drop_unavailable_steps,
    single_step_plan,
)
from zhentu.config import Secret


# ---------------------------------------------------------------- 计划来源标记


def test_single_step_planner_tags_source():
    plan = asyncio.run(SingleStepPlanner().plan({"input": {"messages": []}}))
    assert plan["planner_source"] == "single_step"
    assert "未配置规划模型" in plan["planner_note"]


def test_planner_client_without_config_marks_fallback():
    client = PlannerClient(base_url="", token=None, model="")
    plan = asyncio.run(client.plan({"input": {"messages": []}}))
    assert plan["planner_source"] == "fallback"
    assert "未配置规划模型" in plan["planner_note"]


def test_planner_client_invalid_output_marks_fallback(monkeypatch):
    client = PlannerClient(
        base_url="http://127.0.0.1:1", token=Secret("t"), model="m"
    )

    def boom(request, snapshot, last_error=None):
        return {"steps": []}  # 空步骤 → validate_plan 抛 PlanError

    monkeypatch.setattr(client, "_request_plan", boom)
    plan = asyncio.run(client.plan({"input": {"messages": []}}))
    assert plan["planner_source"] == "fallback"
    assert "planner_invalid" in plan["planner_note"]


def test_planner_client_network_error_marks_fallback(monkeypatch):
    client = PlannerClient(
        base_url="http://127.0.0.1:1", token=Secret("t"), model="m"
    )

    def boom(request, snapshot, last_error=None):
        raise ConnectionError("refused")

    monkeypatch.setattr(client, "_request_plan", boom)
    plan = asyncio.run(client.plan({"input": {"messages": []}}))
    assert plan["planner_source"] == "fallback"
    assert "planner_error" in plan["planner_note"]
    assert "ConnectionError" in plan["planner_note"]


# ---------------------------------------------------------------- 能力快照


def _two_step_plan() -> dict[str, Any]:
    return {
        "plan_id": "p1",
        "max_steps": 2,
        "max_parallel": 2,
        "steps": [
            {
                "id": "s1",
                "goal": "总结",
                "capability": "chat",
                "input": {"messages": []},
                "depends_on": [],
                "required": True,
                "failure_policy": "fail_task",
            },
            {
                "id": "s2",
                "goal": "重排",
                "capability": "rerank",
                "input": {"messages": []},
                "depends_on": ["s1"],
                "required": True,
                "failure_policy": "fail_task",
            },
        ],
        "aggregate": {"mode": "concat", "input_step_ids": ["s1", "s2"]},
    }


def test_drop_unavailable_steps_removes_and_relinks():
    snapshot = {"available": {"chat": [{"model": "qwen"}]}}
    plan = _drop_unavailable_steps(_two_step_plan(), snapshot)
    assert [s["id"] for s in plan["steps"]] == ["s1"]
    assert plan["dropped_steps"] == ["s2"]
    assert plan["aggregate"]["input_step_ids"] == ["s1"]


def test_drop_unavailable_steps_keeps_plan_when_nothing_dropped():
    snapshot = {"available": {"chat": [{"model": "qwen"}], "rerank": [{"model": "r"}]}}
    plan = _two_step_plan()
    assert _drop_unavailable_steps(plan, snapshot) is plan


def test_drop_unavailable_steps_keeps_plan_when_all_unavailable():
    """全部不可用时保留原计划：让运行时给出 no_candidate，错误码更可读。"""
    snapshot = {"available": {"embedding": [{"model": "e"}]}}
    plan = _two_step_plan()
    assert _drop_unavailable_steps(plan, snapshot) is plan


def test_drop_unavailable_steps_without_snapshot_is_noop():
    plan = _two_step_plan()
    assert _drop_unavailable_steps(plan, {}) is plan


def test_planner_prompt_contains_capability_snapshot(monkeypatch):
    """规划请求必须把可用能力带进提示，否则规划器只能靠猜。"""
    captured: dict[str, Any] = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": json.dumps(single_step_plan({"input": {"messages": []}}))}}]}
            ).encode()

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return FakeResp()

    monkeypatch.setattr(orchestration.urllib.request, "urlopen", fake_urlopen)
    client = PlannerClient(base_url="http://127.0.0.1:1", token=Secret("t"), model="m")
    snapshot = {"available": {"chat": [{"model": "qwen2.5:0.5b"}]}}
    plan = asyncio.run(
        client.plan({"input": {"messages": []}, "capability_snapshot": snapshot})
    )
    system_prompt = captured["body"]["messages"][0]["content"]
    assert "qwen2.5:0.5b" in system_prompt
    assert plan["planner_source"] == "planner"


def test_planner_without_snapshot_tells_model_to_stay_single_step(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": json.dumps(single_step_plan({"input": {"messages": []}}))}}]}
            ).encode()

    monkeypatch.setattr(
        orchestration.urllib.request, "urlopen", lambda req, timeout=None: (
            captured.setdefault("body", json.loads(req.data.decode())) or FakeResp()
        )
    )
    client = PlannerClient(base_url="http://127.0.0.1:1", token=Secret("t"), model="m")
    asyncio.run(client.plan({"input": {"messages": []}}))
    system_prompt = captured["body"]["messages"][0]["content"]
    assert "没有可用的能力快照" in system_prompt


# ---------------------------------------------------------------- 格式纠错重试


def _plan_json_str() -> str:
    plan = single_step_plan({"input": {"messages": []}})
    plan.pop("planner_source", None)
    plan.pop("planner_note", None)
    return json.dumps(plan)


def test_retry_recovers_from_invalid_first_attempt(monkeypatch):
    """第一次输出非法、第二次（带着错误反馈）合法：应标记为 planner 且记录重试。"""
    calls: list[str | None] = []

    class FakeResp:
        def __init__(self, content: str) -> None:
            self._content = content

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": self._content}}]}).encode()

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        system_msg = body["messages"][0]["content"]
        # 重试时提示里会带上上一次的校验错误
        calls.append(system_msg)
        if "上一次输出未通过校验" in system_msg:
            return FakeResp(_plan_json_str())
        return FakeResp("这不是 JSON")

    monkeypatch.setattr(orchestration.urllib.request, "urlopen", fake_urlopen)
    client = PlannerClient(base_url="http://127.0.0.1:1", token=Secret("t"), model="m", max_retries=1)
    plan = asyncio.run(client.plan({"input": {"messages": []}}))
    assert plan["planner_source"] == "planner"
    assert "第 2 次尝试成功" in plan.get("planner_note", "")
    assert len(calls) == 2


def test_retry_exhausted_falls_back_with_last_error(monkeypatch):
    calls: list[str | None] = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "仍然不是 JSON"}}]}).encode()

    def fake_urlopen(req, timeout=None):
        calls.append(json.loads(req.data.decode())["messages"][0]["content"])
        return FakeResp()

    monkeypatch.setattr(orchestration.urllib.request, "urlopen", fake_urlopen)
    client = PlannerClient(base_url="http://127.0.0.1:1", token=Secret("t"), model="m", max_retries=2)
    plan = asyncio.run(client.plan({"input": {"messages": []}}))
    assert plan["planner_source"] == "fallback"
    assert "planner_invalid" in plan["planner_note"]
    assert "3 次尝试" in plan["planner_note"]
    assert len(calls) == 3


def test_retry_disabled_makes_single_attempt(monkeypatch):
    calls: list[str | None] = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "bad"}}]}).encode()

    monkeypatch.setattr(
        orchestration.urllib.request, "urlopen",
        lambda req, timeout=None: (calls.append(1), FakeResp())[1],
    )
    client = PlannerClient(base_url="http://127.0.0.1:1", token=Secret("t"), model="m", max_retries=0)
    plan = asyncio.run(client.plan({"input": {"messages": []}}))
    assert plan["planner_source"] == "fallback"
    assert len(calls) == 1


def test_prompt_contains_format_constraints():
    """提示词必须明确键名引号/failure_policy/aggregate.mode，否则弱模型无从遵守。"""
    captured: dict[str, Any] = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": _plan_json_str()}}]}).encode()

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return FakeResp()

    client = PlannerClient(base_url="http://127.0.0.1:1", token=Secret("t"), model="m")
    orchestration.urllib.request.urlopen = fake_urlopen  # noqa: 直接替换以便读取
    try:
        asyncio.run(client.plan({"input": {"messages": []}}))
    finally:
        pass
    system_prompt = captured["body"]["messages"][0]["content"]
    assert "双引号" in system_prompt
    assert "fail_task" in system_prompt
    assert "concat" in system_prompt
