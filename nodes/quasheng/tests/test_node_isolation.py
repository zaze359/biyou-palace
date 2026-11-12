"""阵图（上游）全程不可达时，节点自身能力与启动路径不受影响。

用一个同步阻塞 0.8s 的「死上游」模拟阵图不可达：
- 启动耗时应显著小于该阻塞（首轮发现不得被 await）；
- 本地 chat / 流式 / embeddings / 模型列表 / health 全部正常；
- health 只把上游标 down，节点整体仍 ready。
"""
from __future__ import annotations

import json
import time

import httpx
from fastapi.testclient import TestClient

from nodes.quasheng import app as app_module
from nodes.quasheng.config import Secret, Settings, UpstreamModel, UpstreamSettings
from nodes.quasheng.ollama_client import OllamaClient
from nodes.quasheng.upstream import UpstreamRuntime


class _SlowDeadTransport(httpx.AsyncBaseTransport):
    """阵图不可达：异步等待 0.8s 后连接失败（不阻塞事件循环）。"""

    async def handle_async_request(self, request: httpx.Request):
        import asyncio

        await asyncio.sleep(0.8)
        raise httpx.ConnectError("阵图不可达", request=request)


def _local_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1/models":
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "qwen2.5:0.5b", "object": "model"}]},
        )
    if path == "/api/tags":
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "qwen2.5:0.5b",
                        "details": {"family": "qwen2", "parameter_size": "0.5B"},
                    }
                ]
            },
        )
    if path == "/api/ps":
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:0.5b"}]})
    if path == "/api/version":
        return httpx.Response(200, json={"version": "0.5.7"})
    if path == "/v1/chat/completions":
        payload = json.loads(request.content)
        if payload.get("stream"):
            return httpx.Response(
                200,
                content=(
                    b'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"hi"}}]}\n\n'
                    b"data: [DONE]\n\n"
                ),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "id": "local-1",
                "object": "chat.completion",
                "model": "qwen2.5:0.5b",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "local ok"}}
                ],
            },
        )
    if path == "/v1/embeddings":
        return httpx.Response(
            200, json={"object": "list", "data": [{"embedding": [0.1, 0.2], "index": 0}]}
        )
    return httpx.Response(404, json={"error": {"message": "unexpected"}})


def test_node_fully_usable_when_upstream_unreachable(monkeypatch):
    cfg = UpstreamSettings(
        id="relay-a",
        base_url="https://relay.example.com/v1",
        api_key=Secret("sk-isolation-secret"),
        api_key_env="QUASHENG_UPSTREAM_RELAY_A_KEY",
        enabled=True,
        discover=True,
        models=[UpstreamModel(id="gpt-4o-mini")],
    )
    settings = Settings(
        node_id="isolation-node",
        node_name="隔离测试",
        preload_models=[],
        upstreams=[cfg],
        fallback_mode="auto",
        fallback_chain=["relay-a/gpt-4o-mini", "local/qwen2.5:0.5b"],
        capabilities_map={
            "qwen2.5:0.5b": {"capabilities": ["chat", "embedding"], "context_len": 32768}
        },
    )
    monkeypatch.setattr(app_module, "load_settings", lambda: settings)
    monkeypatch.setattr(
        app_module,
        "OllamaClient",
        lambda base, timeout: OllamaClient(
            base, timeout, transport=httpx.MockTransport(_local_handler)
        ),
    )
    monkeypatch.setattr(
        app_module,
        "UpstreamRuntime",
        lambda upstream_cfg: UpstreamRuntime(
            upstream_cfg, transport=_SlowDeadTransport()
        ),
    )

    app = app_module.create_app()
    started = time.monotonic()
    with TestClient(app, raise_server_exceptions=False) as client:
        elapsed = time.monotonic() - started
        # 首轮发现不得被 await：0.8s 的阻塞不应进入启动耗时
        assert elapsed < 0.6, f"启动被上游发现拖慢：{elapsed:.2f}s"

        models = [item["id"] for item in client.get("/v1/models").json()["data"]]
        assert "qwen2.5:0.5b" in models

        health = client.get("/v1/health").json()
        assert health["status"] == "ready"
        local = [m for m in health["models"] if m.get("source") == "local"]
        assert local and all(m["available"] is True for m in local)
        assert health["upstreams"][0]["status"] == "down"

        chat = client.post(
            "/v1/chat/completions",
            json={"model": "qwen2.5:0.5b", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert chat.status_code == 200
        assert chat.json()["choices"][0]["message"]["content"] == "local ok"

        stream = client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen2.5:0.5b",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert stream.status_code == 200
        assert "data: [DONE]" in stream.text
        assert "upstream_stream_aborted" not in stream.text

        embeddings = client.post(
            "/v1/embeddings", json={"model": "qwen2.5:0.5b", "input": "hi"}
        )
        assert embeddings.status_code == 200
        assert embeddings.json()["data"][0]["embedding"] == [0.1, 0.2]
