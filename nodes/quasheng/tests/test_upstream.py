"""上游桥接 P0 测试：全部用 httpx.MockTransport 模拟本地 Ollama 与远程上游。

覆盖：成功直通（流式/非流式）、4xx/5xx 透传、流中断不补 DONE、
未知/未授权 404、非回环来源 404、密钥零泄漏、未配置上游时行为不变。
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from nodes.quasheng import logbuffer
from nodes.quasheng.app import create_app
from nodes.quasheng.concurrency import ConcurrencyGate
from nodes.quasheng.config import Secret, Settings, UpstreamModel, UpstreamSettings
from nodes.quasheng.ollama_client import OllamaClient
from nodes.quasheng.upstream import UpstreamRuntime

SECRET = "sk-relay-super-secret-do-not-leak"
BASE_URL = "https://relay-a.example.com/v1"

_LOCAL_TAGS = {
    "models": [
        {
            "name": "llama3:8b",
            "details": {"family": "llama", "parameter_size": "8B"},
        }
    ]
}
_LOCAL_PS = {"models": [{"name": "llama3:8b"}]}

SSE_CHUNKS = [
    b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"\xe5\xa5\xbd"}}]}\n\n',
    b"data: [DONE]\n\n",
]


@pytest.fixture(autouse=True)
def _reset_log_ring():
    logbuffer.clear()
    yield
    logbuffer.clear()


class _SSE(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class _AbortSSE(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.ReadError("upstream connection reset")


def _local_ok(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/tags":
        return httpx.Response(200, json=_LOCAL_TAGS)
    if path == "/api/ps":
        return httpx.Response(200, json=_LOCAL_PS)
    if path == "/api/version":
        return httpx.Response(200, json={"version": "0.5.7"})
    if path == "/v1/models":
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "llama3:8b",
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "library",
                    }
                ],
            },
        )
    if path == "/v1/chat/completions":
        return httpx.Response(
            200,
            json={"id": "local-1", "choices": [{"message": {"content": "local ok"}}]},
        )
    return httpx.Response(404, json={"error": {"message": "unexpected", "code": "unexpected"}})


def _upstream_cfg(
    *,
    provider: str = "relay-a",
    visibility: str = "local",
    max_concurrency: int = 2,
    models: list[str] | None = None,
) -> UpstreamSettings:
    return UpstreamSettings(
        id=provider,
        base_url=BASE_URL,
        api_key=Secret(SECRET),
        api_key_env="QUASHENG_UPSTREAM_RELAY_A_KEY",
        connect_timeout_s=1.0,
        timeout_s=5.0,
        max_concurrency=max_concurrency,
        visibility=visibility,
        data_leaves_lan=True,
        enabled=True,
        models=[UpstreamModel(id=model) for model in (models or ["gpt-4o-mini"])],
    )


def _make_app(
    upstream_handler,
    *,
    cfg: UpstreamSettings | None = None,
    local_handler=_local_ok,
    limit: int = 2,
):
    cfg = cfg or _upstream_cfg()
    settings = Settings(
        node_id="test-node",
        node_name="测试节点",
        max_concurrency=limit,
        upstreams=[cfg],
    )
    app = create_app()
    app.state.settings = settings
    app.state.gate = ConcurrencyGate(limit)
    app.state.client = OllamaClient("http://ollama", transport=httpx.MockTransport(local_handler))
    runtime = UpstreamRuntime(cfg, transport=httpx.MockTransport(upstream_handler))
    app.state.upstreams = [runtime]
    return TestClient(app, raise_server_exceptions=False), app, runtime


def _record(handler):
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return wrapped, seen


def _chat_payload(model: str = "relay-a/gpt-4o-mini", *, stream: bool = False) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": stream}


def _remote_ok(stream: bool):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": []})
        if request.url.path == "/v1/chat/completions":
            body = json.loads(request.content)
            if stream:
                return httpx.Response(
                    200,
                    stream=_SSE(SSE_CHUNKS),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "model": body["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                },
            )
        return httpx.Response(404, json={"error": {"message": "unexpected", "code": "unexpected"}})

    return handler


# ---------------------------------------------------------------- 成功直通


def test_remote_non_stream_is_passed_through_with_native_model():
    handler, seen = _record(_remote_ok(stream=False))
    client, app, _runtime = _make_app(handler)

    resp = client.post("/v1/chat/completions", json=_chat_payload())
    assert resp.status_code == 200
    assert resp.json()["model"] == "gpt-4o-mini"
    assert resp.json()["choices"][0]["message"]["content"] == "ok"

    chat = [r for r in seen if r.url.path == "/v1/chat/completions"][0]
    forwarded = json.loads(chat.content)
    # 对外 id 带 provider，转发给上游时换回上游原生 id
    assert forwarded["model"] == "gpt-4o-mini"
    assert forwarded["messages"] == [{"role": "user", "content": "hi"}]
    # 使用节点自持密钥，不转发调用方 Authorization
    assert chat.headers.get("authorization") == f"Bearer {SECRET}"
    assert app.state.gate.active == 0


def test_remote_stream_is_passed_through_chunk_by_chunk():
    handler, seen = _record(_remote_ok(stream=True))
    client, app, _runtime = _make_app(handler)

    resp = client.post("/v1/chat/completions", json=_chat_payload(stream=True))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text == b"".join(SSE_CHUNKS).decode()
    assert resp.text.endswith("data: [DONE]\n\n")
    assert app.state.gate.active == 0

    chat = [r for r in seen if r.url.path == "/v1/chat/completions"][0]
    assert json.loads(chat.content)["stream"] is True


# ---------------------------------------------------------------- 错误透传


@pytest.mark.parametrize("status,code", [(400, "invalid_request"), (404, "model_not_found"), (500, "server_error")])
@pytest.mark.parametrize("stream", [False, True])
def test_upstream_error_status_and_body_are_passed_through(status, code, stream):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            status,
            json={
                "error": {
                    "message": "upstream says no",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": code,
                }
            },
        )

    client, app, _runtime = _make_app(handler)
    resp = client.post("/v1/chat/completions", json=_chat_payload(stream=stream))
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code
    assert resp.json()["error"]["message"] == "upstream says no"
    assert app.state.gate.active == 0


def test_upstream_429_passes_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            429,
            json={"error": {"message": "slow down", "code": "rate_limited"}},
            headers={"retry-after": "7", "x-upstream-secret": SECRET},
        )

    client, _app, runtime = _make_app(handler)
    resp = client.post("/v1/chat/completions", json=_chat_payload())
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "7"
    # 上游响应头不转发（含可能的密钥/内部路由头）
    assert "x-upstream-secret" not in resp.headers
    assert runtime.status == "degraded"


def test_stream_abort_emits_sse_error_without_done():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200,
            stream=_AbortSSE(),
            headers={"content-type": "text/event-stream"},
        )

    client, app, runtime = _make_app(handler)
    resp = client.post("/v1/chat/completions", json=_chat_payload(stream=True))

    assert resp.status_code == 200
    assert b'data: {"choices":[{"delta":{"content":"partial"}}]' in resp.content
    assert b"upstream_stream_aborted" in resp.content
    assert b"[DONE]" not in resp.content
    assert app.state.gate.active == 0
    assert runtime.active == 0


# ---------------------------------------------------------------- 路由与可见性


def test_unknown_remote_model_returns_404_without_calling_upstream():
    handler, seen = _record(_remote_ok(stream=False))
    client, _app, _runtime = _make_app(handler)

    resp = client.post("/v1/chat/completions", json=_chat_payload("relay-a/unknown"))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"
    assert not [r for r in seen if r.url.path == "/v1/chat/completions"]


def test_non_loopback_source_cannot_use_local_visibility_model():
    handler, _seen = _record(_remote_ok(stream=False))
    _client, app, _runtime = _make_app(handler)

    async def scenario() -> tuple[int, int]:
        remote = httpx.ASGITransport(app=app, client=("10.0.0.5", 50000))
        loopback = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=remote, base_url="http://node") as ac:
            outside = await ac.post("/v1/chat/completions", json=_chat_payload())
        async with httpx.AsyncClient(transport=loopback, base_url="http://node") as ac:
            inside = await ac.post("/v1/chat/completions", json=_chat_payload())
        return outside.status_code, inside.status_code

    outside_status, inside_status = asyncio.run(scenario())
    assert outside_status == 404
    assert inside_status == 200


def test_cluster_visibility_allows_non_loopback_and_is_listed():
    handler, _seen = _record(_remote_ok(stream=False))
    client, app, _runtime = _make_app(handler, cfg=_upstream_cfg(visibility="both"))

    async def scenario() -> int:
        remote = httpx.ASGITransport(app=app, client=("10.0.0.5", 50000))
        async with httpx.AsyncClient(transport=remote, base_url="http://node") as ac:
            resp = await ac.post("/v1/chat/completions", json=_chat_payload())
        return resp.status_code

    assert asyncio.run(scenario()) == 200

    models = client.get("/v1/models").json()["data"]
    remote_item = [m for m in models if m["id"] == "relay-a/gpt-4o-mini"][0]
    assert remote_item["owned_by"] == "biyou-node"
    assert remote_item["provider"] == "relay-a"
    assert remote_item["source"] == "upstream"
    assert remote_item["available"] is True


def test_local_visibility_model_is_hidden_from_models_but_visible_in_health():
    handler, _seen = _record(_remote_ok(stream=False))
    client, _app, _runtime = _make_app(handler)

    models = client.get("/v1/models").json()["data"]
    assert [m["id"] for m in models] == ["llama3:8b"]
    assert models[0]["owned_by"] == "biyou-node"

    health = client.get("/v1/health").json()
    remote = [m for m in health["models"] if m.get("source") == "upstream"][0]
    assert remote["name"] == "relay-a/gpt-4o-mini"
    assert remote["provider"] == "relay-a"
    assert remote["available"] is True


def test_health_degrades_when_upstream_unreachable_but_local_stays_up():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client, _app, _runtime = _make_app(handler)
    health = client.get("/v1/health").json()
    assert health["status"] == "degraded"
    remote = [m for m in health["models"] if m.get("source") == "upstream"][0]
    assert remote["available"] is False
    upstream = health["upstreams"][0]
    assert upstream["id"] == "relay-a"
    assert upstream["status"] == "down"
    assert "url" not in upstream and "base_url" not in upstream


def test_health_is_degraded_not_down_when_local_is_down_but_remote_ready():
    def local_down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("local down", request=request)

    client, _app, _runtime = _make_app(_remote_ok(stream=False), local_handler=local_down)
    health = client.get("/v1/health").json()
    assert health["status"] == "degraded"


# ---------------------------------------------------------------- 密钥零泄漏


def test_secret_and_base_url_never_leak(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            500,
            json={
                "error": {
                    "message": f"bad key {SECRET} for {BASE_URL}",
                    "type": "server_error",
                    "code": "upstream_error",
                }
            },
            headers={"x-upstream-secret": SECRET, "set-cookie": "session=leak"},
        )

    client, _app, _runtime = _make_app(handler)
    resp = client.post("/v1/chat/completions", json=_chat_payload())

    assert resp.status_code == 500
    assert SECRET not in resp.text
    assert BASE_URL not in resp.text
    assert SECRET not in json.dumps(dict(resp.headers))
    assert "x-upstream-secret" not in resp.headers
    assert "set-cookie" not in resp.headers

    for path in ("/v1/models", "/v1/health"):
        body = client.get(path).text
        assert SECRET not in body
        assert BASE_URL not in body

    assert SECRET not in caplog.text
    assert SECRET not in json.dumps(logbuffer.recent(50), ensure_ascii=False)


# ---------------------------------------------------------------- 未配置上游


def test_without_upstreams_behavior_is_unchanged():
    settings = Settings(node_id="test-node", node_name="测试节点", max_concurrency=2)
    app = create_app()
    app.state.settings = settings
    app.state.gate = ConcurrencyGate(2)
    app.state.client = OllamaClient("http://ollama", transport=httpx.MockTransport(_local_ok))
    client = TestClient(app, raise_server_exceptions=False)

    health = client.get("/v1/health").json()
    assert "upstreams" not in health
    assert health["status"] == "ready"
    for model in health["models"]:
        assert "provider" not in model
        assert "source" not in model

    models = client.get("/v1/models").json()["data"]
    assert models == [
        {"id": "llama3:8b", "object": "model", "created": 1700000000, "owned_by": "biyou-node"}
    ]

    resp = client.post("/v1/chat/completions", json=_chat_payload("llama3:8b"))
    assert resp.status_code == 200
    assert resp.json()["id"] == "local-1"


def test_remote_embeddings_is_not_bridged():
    handler, seen = _record(_remote_ok(stream=False))
    client, _app, _runtime = _make_app(handler)

    resp = client.post(
        "/v1/embeddings",
        json={"model": "relay-a/gpt-4o-mini", "input": "hello"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"
    assert not [r for r in seen if r.url.path == "/v1/chat/completions"]


def test_upstream_unreachable_returns_503_for_chat():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream down", request=request)

    client, app, runtime = _make_app(handler)
    resp = client.post("/v1/chat/completions", json=_chat_payload())
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "upstream_unavailable"
    assert app.state.gate.active == 0
    assert runtime.status == "down"


# ---------------------------------------------------------------- 上游并发


@pytest.mark.parametrize("stream", [False, True])
def test_upstream_capacity_rejects_immediately_with_429(stream):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200,
            stream=_SSE([b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', b"data: [DONE]\n\n"]),
            headers={"content-type": "text/event-stream"},
        )

    client, app, runtime = _make_app(handler, cfg=_upstream_cfg(max_concurrency=1))
    # 手工占用唯一名额，模拟上一个上游请求仍在途
    assert runtime.try_acquire() is True

    resp = client.post("/v1/chat/completions", json=_chat_payload(stream=stream))
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "upstream_capacity"
    assert resp.headers["retry-after"] == "1"
    assert app.state.gate.active == 0

    runtime.release()
