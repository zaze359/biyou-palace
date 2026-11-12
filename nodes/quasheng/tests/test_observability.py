"""可观测性：Prometheus 指标与日志落盘。"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from nodes.quasheng import logbuffer, metrics
from nodes.quasheng.app import create_app
from nodes.quasheng.concurrency import ConcurrencyGate
from nodes.quasheng.config import Settings, UpstreamModel, UpstreamSettings, Secret
from nodes.quasheng.loading import LoadingTracker
from nodes.quasheng.ollama_client import OllamaClient
from nodes.quasheng.upstream import UpstreamRuntime


def _local(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1/chat/completions":
        payload = json.loads(request.content)
        if payload.get("stream"):
            return httpx.Response(
                200,
                content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "model": "qwen2.5:0.5b",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            },
        )
    if path == "/v1/models":
        return httpx.Response(200, json={"object": "list", "data": [{"id": "qwen2.5:0.5b"}]})
    return httpx.Response(200, json={})


def _app(settings: Settings | None = None, upstream=None) -> TestClient:
    settings = settings or Settings(
        node_id="metrics-node",
        node_name="指标测试",
        capabilities_map={
            "qwen2.5:0.5b": {"capabilities": ["chat", "embedding"], "context_len": 32768}
        },
    )
    app = create_app()
    app.state.settings = settings
    app.state.gate = ConcurrencyGate(2)
    app.state.client = OllamaClient("http://ollama", transport=httpx.MockTransport(_local))
    app.state.loading = LoadingTracker(30)
    app.state.upstreams = upstream or []
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset():
    metrics.reset()
    logbuffer.clear()
    yield
    metrics.reset()
    logbuffer.clear()


def test_metrics_exposes_requests_latency_and_gauges():
    client = _app()
    assert client.post(
        "/v1/chat/completions",
        json={"model": "qwen2.5:0.5b", "messages": [{"role": "user", "content": "hi"}]},
    ).status_code == 200
    assert client.post(
        "/v1/chat/completions",
        json={"model": "qwen2.5:0.5b", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ).status_code == 200

    text = client.get("/metrics").text
    assert 'quasheng_http_requests_total{endpoint="/v1/chat/completions",method="POST",status="200"}' in text
    assert 'quasheng_model_requests_total{model="qwen2.5:0.5b",upstream="local",status="200"} 2' in text
    assert 'quasheng_request_duration_seconds_count{model="qwen2.5:0.5b",upstream="local",stream="false"} 1' in text
    assert 'quasheng_request_duration_seconds_count{model="qwen2.5:0.5b",upstream="local",stream="true"} 1' in text
    assert 'quasheng_first_byte_seconds_count{model="qwen2.5:0.5b",upstream="local",stream="true"} 1' in text
    assert "quasheng_inflight_requests 1" in text  # 含当前 /metrics 抓取本身
    assert "quasheng_gate_active 0" in text
    assert "quasheng_gate_queue_len 0" in text


def test_metrics_fallback_and_upstream_state():
    cfg = UpstreamSettings(
        id="relay-a",
        base_url="https://relay.example.com/v1",
        api_key=Secret("sk-metrics-secret"),
        api_key_env="X",
        enabled=True,
        models=[UpstreamModel(id="gpt-4o-mini")],
    )

    def dead(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(502, json={"error": {"message": "down", "code": "relay_down"}})
        return httpx.Response(503, json={"error": {"message": "down"}})

    settings = Settings(
        node_id="m2",
        capabilities_map={"qwen2.5:0.5b": {"capabilities": ["chat"], "context_len": 8}},
        upstreams=[cfg],
        fallback_mode="auto",
        fallback_chain=["relay-a/gpt-4o-mini", "local/qwen2.5:0.5b"],
    )
    client = _app(settings, [UpstreamRuntime(cfg, transport=httpx.MockTransport(dead))])

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "relay-a/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    text = client.get("/metrics").text
    assert 'quasheng_fallback_total{requested="relay-a/gpt-4o-mini",served_by="local/qwen2.5:0.5b"} 1' in text
    assert 'quasheng_upstream_up{upstream="relay-a"} 0' in text


def test_metrics_endpoint_failure_does_not_break_business(monkeypatch):
    client = _app()
    monkeypatch.setattr(metrics, "render", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    assert client.get("/metrics").status_code == 500
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "qwen2.5:0.5b", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200


def test_logfile_writes_scrubbed_jsonl(tmp_path):
    path = tmp_path / "quasheng.log"
    logbuffer.configure("sk-secret-token", file_path=str(path), max_bytes=10_000, backup_count=2)
    logbuffer.record("INFO", "test", "token=sk-secret-token path=/Users/zaze/private/file.txt")

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event"] == "test"
    assert "sk-secret-token" not in event["message"]
    assert "/Users/zaze" not in event["message"]
    assert "***" in event["message"] and "<path>" in event["message"]
    # /v1/logs 仍只读内存
    assert logbuffer.recent(5)["lines"][-1]["event"] == "test"


def test_logfile_rotation_keeps_configured_backups(tmp_path):
    path = tmp_path / "quasheng.log"
    logbuffer.configure(None, file_path=str(path), max_bytes=300, backup_count=2)
    for index in range(30):
        logbuffer.record("INFO", "rotate", f"line-{index}-" + "x" * 40)

    assert path.exists()
    assert path.with_name("quasheng.log.1").exists()
    assert path.with_name("quasheng.log.2").exists()
    assert not path.with_name("quasheng.log.3").exists()


def test_logfile_failure_degrades_to_memory(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    logbuffer.configure(None, file_path=str(blocker / "quasheng.log"), max_bytes=1024)

    logbuffer.record("INFO", "before-failure", "still works")
    events = [item["event"] for item in logbuffer.recent(10)["lines"]]
    assert "before-failure" in events
    assert "logfile" in events  # 一条降级告警

    logbuffer.record("INFO", "after-failure", "memory only")
    assert logbuffer.recent(5)["lines"][-1]["event"] == "after-failure"
