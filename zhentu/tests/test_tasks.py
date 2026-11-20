from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from zhentu.aggregator import LlmAggregator
from zhentu.app import create_app
from zhentu.config import RegistrySettings, Secret

TOKEN = "test-registry-token"


class Clock:
    def __init__(self) -> None:
        self.ms = 1_700_000_000_000

    def __call__(self) -> int:
        return self.ms


class FakePlanner:
    def __init__(
        self, steps: list[dict[str, Any]] | None = None, *, max_parallel: int = 1
    ) -> None:
        self._steps = steps
        self._max_parallel = max_parallel

    async def plan(self, request: dict[str, Any]) -> dict[str, Any]:
        steps = self._steps or [
            {
                "id": "s1",
                "goal": "回答",
                "capability": "chat",
                "input": {"messages": request["input"]["messages"]},
                "depends_on": [],
                "required": True,
                "failure_policy": "fail_task",
            }
        ]
        return {
            "plan_id": "plan-test",
            "max_steps": len(steps),
            "max_parallel": self._max_parallel,
            "steps": steps,
            "aggregate": {
                "mode": "concat",
                "input_step_ids": [step["id"] for step in steps],
                "include_partial": True,
            },
        }


class FakeAggregator:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def aggregate(self, inputs, *, instructions, timeout_s):
        self.calls.append(list(inputs))
        return "polished:" + "|".join(inputs)


class FakeExecutor:
    def __init__(
        self,
        *,
        fail_steps: set[str] | None = None,
        delay_s: float = 0.0,
        delays: dict[str, float] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.max_active = 0
        self._active = 0
        self._fail_steps = fail_steps or set()
        self._delay_s = delay_s
        self._delays = delays or {}

    async def execute(self, step, route, *, timeout_s):
        self.calls.append(step["id"])
        self._active += 1
        self.max_active = max(self.max_active, self._active)
        try:
            delay = self._delays.get(step["id"], self._delay_s)
            if delay:
                await asyncio.sleep(delay)
            if step["id"] in self._fail_steps:
                return {
                    "status": "failed",
                    "content": None,
                    "output_ref": None,
                    "usage": None,
                    "latency_ms": 1,
                    "error_code": "upstream_error",
                }
            return {
                "status": "ok",
                "content": f"answer:{step['id']}",
                "output_ref": None,
                "usage": {"prompt_tokens": 3, "completion_tokens": 5},
                "latency_ms": 1,
                "error_code": None,
            }
        finally:
            self._active -= 1


def auth(
    node_id: str = "sanxian-01", token: str = TOKEN, role: str = "sanxian"
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Node-Id": node_id,
        "X-Node-Role": role,
        "Contract-Version": "1.0",
    }


def register_node(client: TestClient, node_id: str = "quasheng-01") -> None:
    body = {
        "node": {
            "id": node_id,
            "name": node_id,
            "role": "quasheng",
            "contract_version": "1.0",
        },
        "endpoint": "http://192.168.1.20:4010",
        "status": "ready",
        "models": [
            {
                "name": "qwen2.5:7b",
                "capabilities": ["chat"],
                "context_len": 8192,
                "loaded": True,
            }
        ],
        "limits": {"max_concurrency": 4},
        "resources": {"queue_len": 0, "active": 0},
        "heartbeat_interval_s": 15,
        "boot_id": "boot-12345678",
    }
    response = client.post(
        "/zhentu/v1/nodes/register",
        json=body,
        headers=auth(node_id, role="quasheng"),
    )
    assert response.status_code == 200, response.text


def request_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "schema_version": "1.0",
        "request_id": "req-1",
        "origin_node_id": "sanxian-01",
        "mode": "cluster",
        "requested_mode": "auto",
        "privacy": "normal",
        "deadline_s": 60,
        "input": {"messages": [{"role": "user", "content": "你好"}]},
        "requirements": {"capabilities": ["chat"]},
    }
    body.update(overrides)
    return body


@pytest.fixture()
def client_factory(tmp_path):
    clients: list[TestClient] = []

    def build(
        *,
        planner=None,
        executor=None,
        node_tokens: dict[str, str] | None = None,
        max_active_tasks: int = 4,
        aggregator=None,
    ):
        settings = RegistrySettings(
            host="127.0.0.1",
            port=4020,
            db_path=str(tmp_path / "registry.db"),
            token=Secret(TOKEN) if not node_tokens else None,
            node_tokens=node_tokens or {},
            max_active_tasks=max_active_tasks,
        )
        app = create_app(
            settings,
            clock=Clock(),
            planner=planner or FakePlanner(),
            executor=executor or FakeExecutor(),
            aggregator=aggregator,
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)


def wait_terminal(client: TestClient, task_id: str, timeout_s: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = client.get(f"/zhentu/v1/requests/{task_id}", headers=auth())
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in ("completed", "partial", "failed", "canceled"):
            return payload
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} 未在 {timeout_s}s 内进入终态")


def test_create_and_complete(client_factory):
    client = client_factory()
    register_node(client)
    response = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]

    result = wait_terminal(client, task_id)
    assert result["status"] == "completed"
    assert result["answer"] == "answer:s1"
    assert result["sources"][0]["node_id"] == "quasheng-01"
    assert result["usage"]["prompt_tokens"] == 3


def test_idempotent_request_id(client_factory):
    executor = FakeExecutor()
    client = client_factory(executor=executor)
    register_node(client)
    first = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    second = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["task_id"] == first.json()["task_id"]
    wait_terminal(client, first.json()["task_id"])
    assert executor.calls == ["s1"]


def test_origin_mismatch_and_visibility(client_factory):
    client = client_factory()
    register_node(client)
    created = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    task_id = created.json()["task_id"]
    wait_terminal(client, task_id)

    other = client.get(
        f"/zhentu/v1/requests/{task_id}", headers=auth("sanxian-02")
    )
    assert other.status_code == 403
    events = client.get(
        f"/zhentu/v1/requests/{task_id}/events", headers=auth("sanxian-02")
    )
    assert events.status_code == 403


def test_per_node_tokens_bind_origin(client_factory):
    client = client_factory(node_tokens={"sanxian-01": "tok-1", "sanxian-02": "tok-2"})
    register_node(client)
    wrong = client.post(
        "/zhentu/v1/requests",
        json=request_body(origin_node_id="sanxian-02"),
        headers=auth("sanxian-01", token="tok-1"),
    )
    assert wrong.status_code == 403
    ok = client.post(
        "/zhentu/v1/requests",
        json=request_body(origin_node_id="sanxian-01"),
        headers=auth("sanxian-01", token="tok-1"),
    )
    assert ok.status_code == 202


def test_local_only_rejected(client_factory):
    client = client_factory()
    register_node(client)
    response = client.post(
        "/zhentu/v1/requests", json=request_body(privacy="local_only"), headers=auth()
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "local_only_rejected"


def test_capacity_429(client_factory):
    client = client_factory(executor=FakeExecutor(delay_s=0.5), max_active_tasks=1)
    register_node(client)
    first = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    assert first.status_code == 202
    second = client.post(
        "/zhentu/v1/requests",
        json=request_body(request_id="req-2"),
        headers=auth(),
    )
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "task_capacity_exceeded"


def test_cancel_running(client_factory):
    client = client_factory(executor=FakeExecutor(delay_s=0.2))
    register_node(client)
    created = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    task_id = created.json()["task_id"]
    canceled = client.post(
        f"/zhentu/v1/requests/{task_id}/cancel", headers=auth()
    )
    assert canceled.status_code in (200, 202)
    result = wait_terminal(client, task_id)
    assert result["status"] == "canceled"


def test_required_step_failure(client_factory):
    client = client_factory(executor=FakeExecutor(fail_steps={"s1"}))
    register_node(client)
    created = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    result = wait_terminal(client, created.json()["task_id"])
    assert result["status"] == "failed"
    assert result["terminal_reason"] == "required_step_failed"


def test_optional_step_partial(client_factory):
    steps = [
        {
            "id": "s1",
            "goal": "必须",
            "capability": "chat",
            "input": {"messages": [{"role": "user", "content": "a"}]},
            "depends_on": [],
            "required": True,
            "failure_policy": "fail_task",
        },
        {
            "id": "s2",
            "goal": "可选",
            "capability": "chat",
            "input": {"messages": [{"role": "user", "content": "b"}]},
            "depends_on": ["s1"],
            "required": False,
            "failure_policy": "partial",
        },
    ]
    client = client_factory(planner=FakePlanner(steps), executor=FakeExecutor(fail_steps={"s2"}))
    register_node(client)
    created = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    result = wait_terminal(client, created.json()["task_id"])
    assert result["status"] == "partial"
    assert result["answer"] == "answer:s1"
    assert result["partials"][0]["step_id"] == "s2"


def test_event_replay_and_unknown_last_event(client_factory):
    client = client_factory()
    register_node(client)
    created = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    task_id = created.json()["task_id"]
    wait_terminal(client, task_id)

    response = client.get(f"/zhentu/v1/requests/{task_id}/events", headers=auth())
    assert response.status_code == 200
    assert "task/completed" in response.text
    first_event_id = _first_event_id(response.text)
    assert first_event_id

    resumed = client.get(
        f"/zhentu/v1/requests/{task_id}/events",
        headers={**auth(), "Last-Event-ID": first_event_id},
    )
    assert resumed.status_code == 200
    assert "task/created" not in resumed.text

    missing = client.get(
        f"/zhentu/v1/requests/{task_id}/events",
        headers={**auth(), "Last-Event-ID": "evt-missing"},
    )
    assert missing.status_code == 410


def test_custom_aggregator_used(client_factory):
    aggregator = FakeAggregator()
    client = client_factory(aggregator=aggregator)
    register_node(client)
    created = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    result = wait_terminal(client, created.json()["task_id"])
    assert result["answer"] == "polished:answer:s1"
    assert aggregator.calls == [["answer:s1"]]


def test_llm_aggregator_falls_back_without_config():
    aggregator = LlmAggregator(base_url="", token=None, model="")
    answer = asyncio.run(
        aggregator.aggregate(
            ["第一段", "第二段"], instructions=None, timeout_s=1.0
        )
    )
    assert answer == "第一段\n\n第二段"


def test_parallel_group_runs_concurrently(client_factory):
    executor = FakeExecutor(delay_s=0.1)
    planner = FakePlanner(_parallel_steps(3), max_parallel=3)
    client = client_factory(planner=planner, executor=executor)
    register_node(client)

    created = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    result = wait_terminal(client, created.json()["task_id"])

    assert result["status"] == "completed"
    assert set(executor.calls) == {"s1", "s2", "s3"}
    assert executor.max_active >= 2


def test_parallel_group_respects_max_parallel(client_factory):
    executor = FakeExecutor(delay_s=0.05)
    planner = FakePlanner(_parallel_steps(3), max_parallel=2)
    client = client_factory(planner=planner, executor=executor)
    register_node(client)

    created = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    result = wait_terminal(client, created.json()["task_id"])

    assert result["status"] == "completed"
    assert executor.max_active == 2


def test_parallel_group_aggregates_in_input_order(client_factory):
    aggregator = FakeAggregator()
    executor = FakeExecutor(delays={"s1": 0.1, "s2": 0.01})
    planner = FakePlanner(_parallel_steps(2), max_parallel=2)
    client = client_factory(planner=planner, executor=executor, aggregator=aggregator)
    register_node(client)

    created = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    result = wait_terminal(client, created.json()["task_id"])

    assert result["status"] == "completed"
    assert result["answer"] == "polished:answer:s1|answer:s2"
    assert aggregator.calls == [["answer:s1", "answer:s2"]]


def test_parallel_group_optional_failure_is_partial(client_factory):
    steps = _parallel_steps(2)
    steps[1]["required"] = False
    steps[1]["failure_policy"] = "partial"
    planner = FakePlanner(steps, max_parallel=2)
    executor = FakeExecutor(fail_steps={"s2"})
    client = client_factory(planner=planner, executor=executor)
    register_node(client)

    created = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    result = wait_terminal(client, created.json()["task_id"])

    assert result["status"] == "partial"
    assert result["answer"] == "answer:s1"
    assert result["partials"][0]["step_id"] == "s2"


def test_task_metrics_counts_terminal_and_active(client_factory):
    client = client_factory()
    register_node(client)

    created = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    wait_terminal(client, created.json()["task_id"])

    text = client.get("/zhentu/v1/metrics", headers=auth()).text

    assert "zhentu_tasks_created_total 1" in text
    assert 'zhentu_tasks_terminal_total{status="completed"} 1' in text
    assert 'zhentu_tasks_terminal_total{status="failed"} 0' in text
    assert "zhentu_tasks_active 0" in text
    assert "你好" not in text


def test_task_metrics_active_while_running(client_factory):
    client = client_factory(executor=FakeExecutor(delay_s=0.5))
    register_node(client)

    created = client.post("/zhentu/v1/requests", json=request_body(), headers=auth())
    running_text = client.get("/zhentu/v1/metrics", headers=auth()).text
    assert "zhentu_tasks_active 1" in running_text

    wait_terminal(client, created.json()["task_id"])
    done_text = client.get("/zhentu/v1/metrics", headers=auth()).text
    assert "zhentu_tasks_active 0" in done_text


def _parallel_steps(count: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"s{index}",
            "goal": f"并行 {index}",
            "capability": "chat",
            "input": {"messages": [{"role": "user", "content": f"task-{index}"}]},
            "depends_on": [],
            "parallel_group": "g1",
            "exclusive": False,
            "required": True,
            "failure_policy": "fail_task",
        }
        for index in range(1, count + 1)
    ]


def _first_event_id(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("data: "):
            import json

            payload = json.loads(line[len("data: "):])
            return payload["event_id"]
    return None
