"""`/zhentu/v1/metrics` 的行为测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from zhentu.app import create_app
from zhentu.config import RegistrySettings, Secret

from .test_registry import TOKEN, Clock, auth
from .test_route import _register, _route


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
        yield client


def _metrics(client):
    return client.get("/zhentu/v1/metrics", headers=auth())


def test_metrics_requires_auth(ctx):
    resp = ctx.get("/zhentu/v1/metrics")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_metrics_empty_registry_renders_zero_histogram(ctx):
    resp = _metrics(ctx)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    text = resp.text
    assert "# TYPE zhentu_route_total counter" in text
    assert "# TYPE zhentu_route_duration_seconds histogram" in text
    assert 'zhentu_route_duration_seconds_bucket{le="+Inf"} 0' in text
    assert "zhentu_route_duration_seconds_count 0" in text
    assert "zhentu_route_total{" not in text


def test_metrics_counts_selected_no_route_and_rejected(ctx):
    _register(ctx)
    assert _route(ctx).status_code == 200

    resp = ctx.post(
        "/zhentu/v1/nodes/quasheng-01/heartbeat",
        json={
            "boot_id": "boot-12345678",
            "status": "ready",
            "models_rev": "",
            "loaded_models": ["qwen2.5:7b-instruct-q4_K_M"],
            "resources": {"queue_len": 0, "active": 0, "thermal_shedding": True},
            "limits": {"max_concurrency": 4},
        },
        headers=auth(),
    )
    assert resp.status_code == 200
    assert _route(ctx).json()["reason"] == "no_route"

    text = _metrics(ctx).text

    assert 'zhentu_route_total{strategy="balanced",result="selected"} 1' in text
    assert 'zhentu_route_total{strategy="balanced",result="no_route"} 1' in text
    assert 'zhentu_route_rejected_total{reason="thermal_shedding"} 1' in text
    assert "zhentu_route_duration_seconds_count 2" in text
    # 累计桶语义：两次决策都小于 2.5s，最大有限桶与 +Inf 都必须为 2
    assert 'zhentu_route_duration_seconds_bucket{le="2.5"} 2' in text
    assert 'zhentu_route_duration_seconds_bucket{le="+Inf"} 2' in text


def test_metrics_does_not_leak_topology_or_credentials(ctx):
    _register(ctx)
    _route(ctx)

    text = _metrics(ctx).text

    assert "quasheng-01" not in text
    assert "qwen2.5" not in text
    assert "192.168.1.20" not in text
    assert "endpoint" not in text
    assert TOKEN not in text


def test_metrics_exposes_node_gauges(ctx):
    """节点存量 gauge：注册一个在线节点后 total/online 都为 1，注销后 online 归零。"""
    _register(ctx, node_id="qs-1")

    text = _metrics(ctx).text
    assert "zhentu_nodes_total 1" in text
    assert "zhentu_nodes_online 1" in text

    ctx.post(
        "/zhentu/v1/nodes/qs-1/deregister",
        headers=auth(),
        json={"boot_id": "boot-12345678"},
    )
    text = _metrics(ctx).text
    assert "zhentu_nodes_total 1" in text
    assert "zhentu_nodes_online 0" in text


def test_metrics_node_gauges_absent_of_high_cardinality(ctx):
    """gauge 不得携带 node_id/model 等标签（灵脉只做计数）。"""
    _register(ctx, node_id="qs-1")
    text = _metrics(ctx).text
    for line in text.splitlines():
        if line.startswith("zhentu_nodes_"):
            assert "{" not in line, line
