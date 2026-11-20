"""编排契约 golden fixture 的 Python 侧校验。

fixture 位于 docs/fixtures/orchestration/envelopes.v1.json，供 Python / Kotlin
两端共享；当前只校验 Python 侧结构与枚举，Kotlin 端待编排模型实现后接入。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "fixtures"
    / "orchestration"
    / "envelopes.v1.json"
)

TASK_EVENT_TYPES = {
    "task/created",
    "task/planned",
    "step/started",
    "step/result",
    "step/failed",
    "step/canceled",
    "step/retrying",
    "aggregate/started",
    "aggregate/result",
    "task/completed",
    "task/failed",
    "task/canceled",
    "task/interrupted",
    "task/resumed",
}


@pytest.fixture(scope="module")
def fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_request_envelope(fixture):
    request = fixture["request_envelope"]
    assert request["schema_version"] == fixture["schema_version"]
    assert request["mode"] in ("local", "cluster")
    assert request["requested_mode"] in ("local", "cluster", "auto")
    assert request["privacy"] in ("normal", "local_only")
    assert 5 <= request["deadline_s"] <= 600
    assert request["input"]["messages"]


def test_plan(fixture):
    plan = fixture["plan"]
    steps = plan["steps"]
    ids = [step["id"] for step in steps]
    assert len(ids) == len(set(ids))
    assert plan["max_steps"] >= len(steps)
    assert plan["max_parallel"] >= 1

    known: set[str] = set()
    for step in steps:
        assert step["failure_policy"] in ("fail_task", "partial")
        assert isinstance(step["required"], bool)
        assert step["input"]["messages"]
        assert all(dep in known for dep in step["depends_on"])
        known.add(step["id"])

    assert all(
        step_id in known for step_id in plan["aggregate"]["input_step_ids"]
    )


def test_task_event(fixture):
    event = fixture["task_event"]
    assert event["event_id"]
    assert event["task_id"] == fixture["result_envelope"]["task_id"]
    assert isinstance(event["seq"], int) and event["seq"] >= 1
    assert event["type"] in TASK_EVENT_TYPES
    assert event["data"]["step_id"] in {
        step["id"] for step in fixture["plan"]["steps"]
    }


def test_result_envelope(fixture):
    result = fixture["result_envelope"]
    assert result["schema_version"] == fixture["schema_version"]
    assert result["status"] in ("completed", "partial", "failed", "canceled")
    assert result["mode"] in ("local", "cluster")
    assert isinstance(result["sources"], list)
    assert isinstance(result["partials"], list)
    if result["error"] is not None:
        assert result["error"]["type"] == "orchestration_error"
