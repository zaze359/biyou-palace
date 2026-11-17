"""`POST /zhentu/v1/route` 的行为测试。

覆盖：鉴权、确定性排序、硬过滤（租约/状态/模型/上下文/排除/降载/限流）、
缺字段退化、无候选、参数错误与脱敏。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from zhentu.app import create_app
from zhentu.config import RegistrySettings, Secret

from .test_registry import TOKEN, Clock, auth, register_body


@pytest.fixture
def ctx(tmp_path):
    clock = Clock()
    settings = RegistrySettings(
        host="127.0.0.1",
        port=4020,
        db_path=str(tmp_path / "registry.db"),
        token=Secret(TOKEN),
    )
    app = create_app(settings, clock=clock)
    with TestClient(app) as client:
        yield client, clock


def _register(client, *, limits=None, resources=None, **kwargs):
    body = register_body(**kwargs)
    if limits is not None:
        body["limits"] = limits
    if resources is not None:
        body["resources"] = resources
    resp = client.post(
        "/zhentu/v1/nodes/register",
        json=body,
        headers=auth(body["node"]["id"], body["node"]["role"]),
    )
    assert resp.status_code == 200, resp.text
    return body


def _route(client, **body):
    return client.post("/zhentu/v1/route", json=body, headers=auth())


def _chat_model(name: str = "qwen2.5:7b", *, loaded: bool = True, context_len=None):
    return {
        "name": name,
        "capabilities": ["chat"],
        "context_len": context_len,
        "loaded": loaded,
    }


def test_route_requires_auth(ctx):
    client, _ = ctx
    resp = client.post("/zhentu/v1/route", json={"capability": "chat"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_route_returns_decision_shape(ctx):
    client, _ = ctx
    _register(client)

    resp = _route(client)

    assert resp.status_code == 200
    data = resp.json()
    assert data["registry_version"] == "1.0"
    assert data["ttl_ms"] == 5000
    assert data["decision_id"].startswith("dec-")
    assert data["decided_at"] == data["server_time"]
    selected = data["selected"]
    assert selected["node_id"] == "quasheng-01"
    assert selected["model"] == "qwen2.5:7b-instruct-q4_K_M"
    assert selected["role"] == "quasheng"
    assert selected["capabilities"] == ["chat"]
    assert isinstance(selected["score"], float)
    assert 0.0 <= selected["score"] <= 1.0
    assert "loaded" in selected["reasons"]
    assert data["alternates"] == []
    # embedding 模型不参与 chat 决策
    assert data["rejected"] == []


def test_route_is_deterministic(ctx):
    client, _ = ctx
    _register(client, node_id="quasheng-01")
    _register(
        client,
        node_id="quasheng-02",
        queue_len=1,
        endpoint="http://192.168.1.21:4010",
    )

    first = _route(client).json()
    second = _route(client).json()

    assert first["selected"] == second["selected"]
    assert first["alternates"] == second["alternates"]
    assert first["rejected"] == second["rejected"]
    assert first["decision_id"] != second["decision_id"]


def test_route_rejects_offline_and_deregistered(ctx):
    client, clock = ctx
    _register(client, node_id="quasheng-01")
    _register(client, node_id="quasheng-02", endpoint="http://192.168.1.21:4010")

    clock.advance(120)  # 超过准圣 90s 租约
    _register(client, node_id="quasheng-02", endpoint="http://192.168.1.21:4010")
    resp = client.post(
        "/zhentu/v1/nodes/quasheng-02/deregister",
        json={"boot_id": "boot-12345678"},
        headers=auth("quasheng-02"),
    )
    assert resp.status_code == 200

    data = _route(client).json()

    assert data["selected"] is None
    assert data["reason"] == "no_route"
    reasons = {(item["node_id"], item["reason"]) for item in data["rejected"]}
    assert ("quasheng-01", "offline") in reasons
    assert ("quasheng-02", "deregistered") in reasons


def test_route_status_filters_and_loading_flag(ctx):
    client, _ = ctx
    _register(client, status="down")
    data = _route(client).json()
    assert data["selected"] is None
    assert data["rejected"] == [
        {"node_id": "quasheng-01", "model": "qwen2.5:7b-instruct-q4_K_M", "reason": "status_down"}
    ]

    client, _ = ctx
    _register(client, status="loading")
    assert _route(client).json()["selected"] is None
    data = _route(client, allow_loading=True).json()
    assert data["selected"]["node_id"] == "quasheng-01"


def test_route_degraded_is_allowed_but_ranked_below_ready(ctx):
    client, _ = ctx
    _register(client, node_id="quasheng-02", status="degraded", endpoint="http://192.168.1.21:4010")
    _register(client, node_id="quasheng-01")

    data = _route(client).json()

    assert data["selected"]["node_id"] == "quasheng-01"
    assert [item["node_id"] for item in data["alternates"]] == ["quasheng-02"]


def test_route_model_filters_and_newapi_prefix(ctx):
    client, _ = ctx
    _register(client)

    only = _route(client, model="qwen2.5:7b-instruct-q4_K_M").json()
    assert only["selected"]["model"] == "qwen2.5:7b-instruct-q4_K_M"

    missing = _route(client, model="not-installed").json()
    assert missing["selected"] is None
    assert missing["rejected"] == []

    allowed = _route(client, allowed_models=["newapi/qwen2.5:7b-instruct-q4_K_M"]).json()
    assert allowed["selected"]["model"] == "qwen2.5:7b-instruct-q4_K_M"

    excluded = _route(client, exclude_models=["newapi/qwen2.5:7b-instruct-q4_K_M"]).json()
    assert excluded["selected"] is None
    assert excluded["rejected"][0]["reason"] == "model_excluded"

    excluded_node = _route(client, exclude_nodes=["quasheng-01"]).json()
    assert excluded_node["selected"] is None
    assert excluded_node["rejected"][0]["reason"] == "node_excluded"


def test_route_context_length_filter_and_unknown(ctx):
    client, _ = ctx
    _register(
        client,
        models=[
            _chat_model("short", context_len=4096, loaded=True),
            _chat_model("long", context_len=32768, loaded=False),
        ],
    )

    data = _route(client, context_len=8192).json()

    assert [item["model"] for item in [data["selected"], *data["alternates"]]] == ["long"]
    assert data["rejected"] == [
        {"node_id": "quasheng-01", "model": "short", "reason": "context_too_small"}
    ]

    client, _ = ctx
    _register(client, models=[_chat_model("unknown", context_len=None, loaded=True)])
    data = _route(client, context_len=99999).json()
    assert data["selected"]["model"] == "unknown"
    assert "context_unknown" in data["selected"]["reasons"]


def test_route_missing_resources_and_limits_still_decides(ctx):
    client, _ = ctx
    _register(
        client,
        models=[_chat_model("bare", loaded=False)],
        limits={},
        resources={},
    )

    resp = _route(client)

    assert resp.status_code == 200
    selected = resp.json()["selected"]
    assert selected["model"] == "bare"
    assert "estimated_load" in selected["reasons"]
    assert "cold" in selected["reasons"]


def test_route_missing_active_falls_back_without_double_counting(ctx):
    """v1.2：in_flight = active + queue_len；旧节点缺 active 时按 queue_len 估算，不得重复相加。"""
    client, _ = ctx
    limits = {"max_concurrency": 10}
    _register(
        client,
        node_id="modern-01",
        resources={"queue_len": 0, "active": 2},
        limits=limits,
    )
    _register(
        client,
        node_id="legacy-01",
        resources={"queue_len": 2},
        limits=limits,
    )

    data = _route(client).json()
    entries = {
        item["node_id"]: item
        for item in [data["selected"], *data["alternates"]]
        if item is not None
    }

    assert set(entries) == {"modern-01", "legacy-01"}
    assert entries["modern-01"]["score"] == entries["legacy-01"]["score"]
    assert "estimated_load" in entries["legacy-01"]["reasons"]
    assert "estimated_load" not in entries["modern-01"]["reasons"]


def test_route_thermal_shedding_and_limit_zero(ctx):
    client, _ = ctx
    _register(client, models=[_chat_model("shedding", loaded=True)])
    # 心跳把热降载覆盖到运行态
    resp = client.post(
        "/zhentu/v1/nodes/quasheng-01/heartbeat",
        json={
            "boot_id": "boot-12345678",
            "status": "ready",
            "models_rev": "",
            "loaded_models": ["shedding"],
            "resources": {"queue_len": 0, "active": 0, "thermal_shedding": True},
            "limits": {"max_concurrency": 4},
        },
        headers=auth(),
    )
    assert resp.status_code == 200

    data = _route(client).json()
    assert data["selected"] is None
    assert data["rejected"][0]["reason"] == "thermal_shedding"

    data = _route(client, allow_shedding=True).json()
    assert data["selected"]["model"] == "shedding"

    client, _ = ctx
    _register(client, models=[_chat_model("zero", loaded=True)], limits={"max_concurrency": 0})
    data = _route(client).json()
    assert data["selected"] is None
    assert data["rejected"][0]["reason"] == "limit_zero"


def test_route_no_candidates_is_200(ctx):
    client, _ = ctx

    resp = _route(client, capability="rerank")

    assert resp.status_code == 200
    data = resp.json()
    assert data["selected"] is None
    assert data["alternates"] == []
    assert data["reason"] == "no_route"


def test_route_invalid_params(ctx):
    client, _ = ctx

    assert _route(client, strategy="fastest").json()["error"]["code"] == "invalid_strategy"
    assert _route(client, capability="bad cap").json()["error"]["code"] == "invalid_capability"
    too_many = _route(client, allowed_models=[f"m{index}" for index in range(201)])
    assert too_many.status_code == 400
    assert too_many.json()["error"]["code"] == "too_many_models"
    bad_context = _route(client, context_len=0)
    assert bad_context.status_code == 422
    assert bad_context.json()["error"]["code"] == "invalid_payload"


def test_route_max_candidates_clamped(ctx):
    client, _ = ctx
    _register(client, models=[_chat_model(f"m{index}", loaded=True) for index in range(6)])

    data = _route(client, max_candidates=99).json()

    assert len([data["selected"], *data["alternates"]]) == 6
    assert len(data["alternates"]) == 5

    # 下限夹到 1：0/负数只返回 selected，不返回 alternates
    smallest = _route(client, max_candidates=0).json()
    assert smallest["selected"] is not None
    assert smallest["alternates"] == []


def test_route_never_leaks_endpoint_or_token(ctx):
    client, _ = ctx
    _register(client)

    resp = _route(client)

    text = resp.text
    assert "192.168.1.20" not in text
    assert "endpoint" not in text
    assert TOKEN not in text
    assert all("endpoint" not in item for item in resp.json()["rejected"])


def test_route_storage_failure_returns_503(ctx, monkeypatch):
    client, _ = ctx

    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(client.app.state.store, "list_all", boom)

    resp = _route(client)

    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["code"] == "registry_unavailable"
    assert error["type"] == "server_error"
