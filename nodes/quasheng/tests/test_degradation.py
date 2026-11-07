"""降级与探针测试（Q6）：不依赖真实 Ollama，用 httpx.MockTransport 模拟上游。

覆盖四类场景：
1. 启动探针上游不可达 → 抛 UpstreamUnavailable，消息含「Ollama ≥ 0.5.x」
2. 运行期上游断连 → /v1/health 仍 200 且 status=down；chat 返回 503 OpenAI 错误体
3. psutil 采集抛异常 → resources 字段降级为 null，接口不返回 500
4. 预热 /api/generate 返回 400 → 自动降级走 /api/chat 且成功
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from nodes.quasheng import app as app_module
from nodes.quasheng.app import _probe_with_retry, _warmup, create_app
from nodes.quasheng.concurrency import ConcurrencyGate
from nodes.quasheng.config import Settings
from nodes.quasheng.errors import UpstreamUnavailable
from nodes.quasheng.ollama_client import OllamaClient


def _build_client(handler, *, preload: list[str] | None = None):
    settings = Settings(
        node_id="test-node",
        node_name="测试节点",
        max_concurrency=2,
        preload_models=preload or [],
    )
    app = create_app()
    app.state.settings = settings
    app.state.gate = ConcurrencyGate(settings.max_concurrency)
    app.state.client = OllamaClient("http://upstream", transport=httpx.MockTransport(handler))
    return TestClient(app, raise_server_exceptions=False), app


def _raise_connect(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


def _ok_upstream(request: httpx.Request) -> httpx.Response:
    if request.url.path in ("/api/tags", "/api/ps"):
        return httpx.Response(200, json={"models": []})
    if request.url.path == "/api/version":
        return httpx.Response(200, json={"version": "0.5.7"})
    return httpx.Response(200, json={"ok": True})


# ---------------------------------------------------------------- 1. 启动探针


def test_probe_unreachable_raises_with_version_hint():
    client = OllamaClient("http://upstream", transport=httpx.MockTransport(_raise_connect))
    with pytest.raises(UpstreamUnavailable) as excinfo:
        asyncio.run(client.probe())
    assert "Ollama ≥ 0.5.x" in excinfo.value.message
    assert excinfo.value.status == 503
    assert excinfo.value.code == "upstream_unavailable"


def test_probe_old_ollama_without_v1_raises_with_version_hint():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="404 page not found")

    client = OllamaClient("http://upstream", transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamUnavailable) as excinfo:
        asyncio.run(client.probe())
    assert "Ollama ≥ 0.5.x" in excinfo.value.message
    assert "HTTP 404" in excinfo.value.message


def test_lifespan_aborts_startup_when_probe_fails(monkeypatch):
    """启动探针失败必须让 lifespan 抛错，节点不会带病启动。"""
    settings = Settings(preload_models=[])
    monkeypatch.setattr(app_module, "PROBE_ATTEMPTS", 2)
    monkeypatch.setattr(app_module, "PROBE_BASE_DELAY", 0.0)
    monkeypatch.setattr(app_module, "load_settings", lambda: settings)
    monkeypatch.setattr(
        app_module,
        "OllamaClient",
        lambda base, timeout: OllamaClient(
            base, timeout, transport=httpx.MockTransport(_raise_connect)
        ),
    )
    app = create_app()
    with pytest.raises(UpstreamUnavailable):
        with TestClient(app):
            pass


def test_probe_retry_recovers_before_exhausting_attempts(monkeypatch):
    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        async def probe(self) -> None:
            self.calls += 1
            if self.calls < 3:
                raise UpstreamUnavailable("Ollama 未就绪")

    monkeypatch.setattr(app_module, "PROBE_BASE_DELAY", 0.0)
    flaky = Flaky()
    asyncio.run(_probe_with_retry(flaky, attempts=3))
    assert flaky.calls == 3


def test_probe_retry_exhausts_finite_attempts(monkeypatch):
    class Down:
        def __init__(self) -> None:
            self.calls = 0

        async def probe(self) -> None:
            self.calls += 1
            raise UpstreamUnavailable("Ollama 未就绪")

    monkeypatch.setattr(app_module, "PROBE_BASE_DELAY", 0.0)
    down = Down()
    with pytest.raises(UpstreamUnavailable):
        asyncio.run(_probe_with_retry(down, attempts=3))
    assert down.calls == 3


# ---------------------------------------------------------------- 2. 运行期断连


def test_health_down_and_chat_503_when_upstream_dies_at_runtime():
    client, app = _build_client(_raise_connect)

    health = client.get("/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "down"
    assert health.json()["models"] == []

    chat = client.post(
        "/v1/chat/completions",
        json={"model": "mock-7b", "messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert chat.status_code == 503
    err = chat.json()["error"]
    assert err["type"] == "server_error"
    assert err["code"] == "upstream_unavailable"
    assert err["param"] is None
    assert app.state.gate.active == 0

    # 进程未崩：后续健康检查依旧可用
    assert client.get("/v1/health").status_code == 200


def test_health_auto_recovers_to_ready_after_upstream_returns():
    state = {"down": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["down"]:
            raise httpx.ConnectError("connection refused")
        return _ok_upstream(request)

    client, _ = _build_client(handler)
    assert client.get("/v1/health").json()["status"] == "down"

    state["down"] = False
    assert client.get("/v1/health").json()["status"] == "ready"


def test_stream_chat_503_when_upstream_dies_at_runtime():
    client, app = _build_client(_raise_connect)
    chat = client.post(
        "/v1/chat/completions",
        json={"model": "mock-7b", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert chat.status_code == 503
    assert chat.json()["error"]["code"] == "upstream_unavailable"
    assert app.state.gate.active == 0


# ---------------------------------------------------------------- 3. psutil 降级


def test_psutil_failure_degrades_resources_to_null(monkeypatch):
    from nodes.quasheng import health as health_module

    class BrokenPsutil:
        @staticmethod
        def cpu_percent(interval=None):
            raise RuntimeError("cpu_percent boom")

        @staticmethod
        def virtual_memory():
            raise RuntimeError("virtual_memory boom")

        @staticmethod
        def disk_usage(path):
            raise RuntimeError("disk_usage boom")

    monkeypatch.setattr(health_module, "psutil", BrokenPsutil)

    client, _ = _build_client(_ok_upstream)
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    resources = resp.json()["resources"]
    assert resources["cpu_pct"] is None
    assert resources["mem_used_mb"] is None
    assert resources["mem_total_mb"] is None
    assert resources["disk_free_mb"] is None
    assert isinstance(resources["queue_len"], int)


# ---------------------------------------------------------------- 4. 预热降级


def test_warmup_falls_back_to_api_chat_when_generate_rejected(caplog):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/generate":
            return httpx.Response(400, json={"error": "empty prompt not allowed"})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "ok"}})

    client = OllamaClient("http://upstream", transport=httpx.MockTransport(handler))
    caplog.set_level(logging.INFO, logger="quasheng")
    asyncio.run(_warmup(client, Settings(preload_models=["mock-7b"])))

    assert calls == ["/api/generate", "/api/chat"]
    messages = [r.getMessage() for r in caplog.records if r.name == "quasheng"]
    assert any("预热模型 mock-7b 完成：路径=/api/chat" in m for m in messages)
    assert not any("失败" in m or "异常" in m for m in messages)
