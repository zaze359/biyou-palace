"""端点级测试：用 httpx.MockTransport 模拟 Ollama，不依赖真实服务。

覆盖 Q1（流式错误状态码透传）、Q2（422/404/500 错误归一 + X-Node-* 头）、
Q3（INFO 日志可见、预热耗时日志、断连日志）。
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from nodes.quasheng import app as app_module
from nodes.quasheng.app import _stream_body, _warmup, create_app
from nodes.quasheng.concurrency import ConcurrencyGate
from nodes.quasheng.config import Settings
from nodes.quasheng.ollama_client import OllamaClient

SSE_BODY = (
    'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"世界"}}]}\n\n'
    "data: [DONE]\n\n"
)


def _build_client(handler, *, auth: bool = False, token: str | None = None, limit: int = 2, **settings_kwargs):
    settings = Settings(
        node_id="test-node",
        node_name="测试节点",
        auth_enabled=auth,
        shared_token=token,
        max_concurrency=limit,
        **settings_kwargs,
    )
    app = create_app()
    app.state.settings = settings
    app.state.gate = ConcurrencyGate(limit)
    app.state.client = OllamaClient("http://upstream", transport=httpx.MockTransport(handler))
    return TestClient(app, raise_server_exceptions=False), app


def _ok_upstream(request: httpx.Request) -> httpx.Response:
    if request.url.path in ("/api/tags", "/api/ps"):
        return httpx.Response(200, json={"models": []})
    if request.url.path == "/api/version":
        return httpx.Response(200, json={"version": "0.5.7"})
    return httpx.Response(200, json={"ok": True})


# ---------------------------------------------------------------- Q1


def test_stream_upstream_404_is_passed_through_as_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "error": {
                    "message": "model 'nope' not found",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "model_not_found",
                }
            },
        )

    client, app = _build_client(handler)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "nope", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    err = resp.json()["error"]
    assert err["message"] == "model 'nope' not found"
    assert err["code"] == "model_not_found"
    assert err["param"] is None
    # 名额必须在错误返回时释放
    assert app.state.gate.active == 0


def test_stream_upstream_500_plain_text_is_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal boom")

    client, app = _build_client(handler)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [], "stream": True},
    )
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["message"] == "internal boom"
    assert body["error"]["type"] == "server_error"
    assert body["error"]["code"] == "upstream_error"
    assert app.state.gate.active == 0


def test_stream_upstream_200_keeps_chunks_and_done():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SSE_BODY, headers={"content-type": "text/event-stream"})

    client, app = _build_client(handler)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text == SSE_BODY
    assert resp.text.endswith("data: [DONE]\n\n")
    assert app.state.gate.active == 0


# ---------------------------------------------------------------- Q2


@pytest.mark.parametrize("path", ["/v1/health", "/v1/models"])
def test_node_headers_present_on_success(path):
    client, _ = _build_client(_ok_upstream)
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.headers["X-Node-Id"] == "test-node"
    assert resp.headers["X-Node-Role"] == "quasheng"
    assert resp.headers["Contract-Version"] == "1.0"


def test_invalid_json_returns_openai_style_422():
    client, _ = _build_client(_ok_upstream)
    resp = client.post(
        "/v1/chat/completions",
        content="{bad json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert set(err) == {"message", "type", "param", "code"}
    assert err["type"] == "invalid_request_error"
    assert err["code"] == "invalid_request_error"
    assert resp.headers["X-Node-Id"] == "test-node"
    assert resp.headers["Contract-Version"] == "1.0"


def test_unknown_route_returns_openai_style_404():
    client, _ = _build_client(_ok_upstream)
    resp = client.get("/v1/nope")
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["code"] == "not_found"
    assert err["param"] is None
    assert resp.headers["X-Node-Id"] == "test-node"


def test_unexpected_exception_returns_openai_style_500_with_headers():
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("boom")

    client, _ = _build_client(handler)
    resp = client.get("/v1/models")
    assert resp.status_code == 500
    err = resp.json()["error"]
    assert err["type"] == "server_error"
    assert err["code"] == "internal_error"
    assert resp.headers["X-Node-Id"] == "test-node"
    assert resp.headers["X-Node-Role"] == "quasheng"


def test_auth_and_capability_error_shapes_unchanged():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client, _ = _build_client(handler, auth=True, token="t")
    unauthenticated = client.get("/v1/health")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "invalid_api_key"

    bad_token = client.get("/v1/health", headers={"Authorization": "Bearer wrong"})
    assert bad_token.status_code == 401
    assert bad_token.json()["error"]["code"] == "invalid_api_key"

    embeddings = client.post(
        "/v1/embeddings",
        json={"model": "plain-chat", "input": ["x"]},
        headers={"Authorization": "Bearer t"},
    )
    assert embeddings.status_code == 501
    assert embeddings.json()["error"]["code"] == "capability_not_supported"


# ---------------------------------------------------------------- Q3


def test_quasheng_logger_emits_info():
    create_app()
    qs_logger = logging.getLogger("quasheng")
    assert qs_logger.isEnabledFor(logging.INFO)
    assert qs_logger.handlers, "quasheng logger 需要自带 handler，否则 uvicorn 下 INFO 不可见"


def test_warmup_logs_elapsed_ms_per_model(caplog):
    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self) -> None:
            self.paths: list[str] = []

        async def post(self, path: str, payload: dict) -> FakeResp:
            self.paths.append(path)
            return FakeResp()

    settings = Settings(preload_models=["m1", "m2"])
    fake = FakeClient()
    caplog.set_level(logging.INFO, logger="quasheng")
    asyncio.run(_warmup(fake, settings))

    messages = [r.getMessage() for r in caplog.records if r.name == "quasheng"]
    assert fake.paths == ["/api/generate", "/api/generate"]
    assert sum("耗时" in m and "ms" in m for m in messages) >= 2
    assert any("m1" in m and "路径=/api/generate" in m for m in messages)
    assert any("m2" in m for m in messages)


def test_warmup_logs_fallback_path(caplog):
    class FakeResp:
        def __init__(self, status: int) -> None:
            self.status_code = status

    class FakeClient:
        async def post(self, path: str, payload: dict) -> FakeResp:
            return FakeResp(400 if path == "/api/generate" else 200)

    caplog.set_level(logging.INFO, logger="quasheng")
    asyncio.run(_warmup(FakeClient(), Settings(preload_models=["m1"])))

    messages = [r.getMessage() for r in caplog.records if r.name == "quasheng"]
    assert any("路径=/api/chat" in m and "耗时" in m for m in messages)


def test_disconnect_logs_and_releases_slot(caplog):
    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return True

    class FakeUpstream:
        async def aiter_bytes(self):
            yield b"data: first\n\n"

    class FakeStack:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    async def scenario() -> None:
        gate = ConcurrencyGate(limit=1)
        await gate.acquire()
        stack = FakeStack()
        gen = _stream_body(FakeRequest(), FakeUpstream(), stack, gate)
        received = [chunk async for chunk in gen]
        assert received == []
        assert gate.active == 0
        assert stack.closed is True

    caplog.set_level(logging.INFO, logger="quasheng")
    asyncio.run(scenario())
    assert any("中止上游生成" in r.getMessage() for r in caplog.records if r.name == "quasheng")


def test_generator_close_logs_abort(caplog):
    """模拟 Starlette 因客户端断开而直接关闭流生成器。"""

    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return False

    class FakeUpstream:
        async def aiter_bytes(self):
            yield b"chunk-1"
            yield b"chunk-2"

    class FakeStack:
        async def aclose(self) -> None:
            pass

    async def scenario() -> None:
        gate = ConcurrencyGate(limit=1)
        await gate.acquire()
        gen = _stream_body(FakeRequest(), FakeUpstream(), FakeStack(), gate)
        first = await gen.__anext__()
        assert first == b"chunk-1"
        await gen.aclose()
        assert gate.active == 0

    caplog.set_level(logging.INFO, logger="quasheng")
    asyncio.run(scenario())
    assert any("中止上游生成" in r.getMessage() for r in caplog.records if r.name == "quasheng")


def test_upstream_error_body_normalization_variants():
    from nodes.quasheng.errors import normalize_upstream_error

    assert normalize_upstream_error(404, b"")["error"]["message"] == "上游返回 HTTP 404"
    assert normalize_upstream_error(429, b'{"error": "slow down"}')["error"]["code"] == "upstream_error"
    assert normalize_upstream_error(502, b"bad gateway")["error"]["message"] == "bad gateway"


def test_non_stream_upstream_error_status_is_preserved():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad input", "type": "invalid_request_error", "code": "bad"}})

    client, app = _build_client(handler)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [], "stream": False},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "bad input"
    assert app.state.gate.active == 0
    # 确认模块常量未被测试无意改写
    assert app_module.NODE_ROLE == "quasheng"


# ---------------------------------------------------------------- Q5：health / models / 并发 / queue_len

_UPSTREAM_TAGS = {
    "models": [
        {
            "name": "qwen2.5:7b",
            "model": "qwen2.5:7b",
            "details": {"family": "qwen2", "parameter_size": "7B"},
        },
        {
            "name": "nomic-embed-text",
            "model": "nomic-embed-text",
            "details": {"family": "nomic", "parameter_size": "137M"},
        },
    ]
}
_UPSTREAM_PS = {"models": [{"name": "qwen2.5:7b", "size_vram": 6_000_000_000}]}


def _health_upstream(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/tags":
        return httpx.Response(200, json=_UPSTREAM_TAGS)
    if request.url.path == "/api/ps":
        return httpx.Response(200, json=_UPSTREAM_PS)
    if request.url.path == "/api/version":
        return httpx.Response(200, json={"version": "0.5.7"})
    return httpx.Response(404, json={"error": "unexpected path"})


def test_health_full_contract_structure():
    client, app = _build_client(
        _health_upstream,
        limit=3,
        capabilities_map={
            "qwen2.5:7b": {"capabilities": ["chat"], "context_len": 32768},
            "nomic-embed-text": {"capabilities": ["embedding"], "context_len": 8192},
        },
        mem_warn_pct=100,
        cpu_warn_pct=100,
    )
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()

    node = body["node"]
    assert node["id"] == "test-node"
    assert node["name"] == "测试节点"
    assert node["role"] == "quasheng"
    assert node["runtime"] == "ollama"
    assert node["runtime_version"] == "0.5.7"
    assert node["contract_version"] == "1.0"
    assert node["arch"]

    assert body["status"] == "ready"
    assert body["limits"] == {"max_concurrency": 3, "max_context_len": 32768}
    assert body["updated_at"].endswith(("+00:00", "+08:00")) or body["updated_at"].count(":") >= 2

    # 准圣专属：电池/温度/热状态恒为 null
    resources = body["resources"]
    assert resources["battery_pct"] is None
    assert resources["temperature_c"] is None
    assert resources["thermal_state"] is None
    assert resources["queue_len"] == 0
    for key in ("cpu_pct", "mem_used_mb", "mem_total_mb", "disk_free_mb"):
        assert key in resources

    # models[].loaded 必须与 mock /api/ps 一致
    loaded_names = {m["name"] for m in _UPSTREAM_PS["models"]}
    assert [m["name"] for m in body["models"]] == ["qwen2.5:7b", "nomic-embed-text"]
    for model in body["models"]:
        assert model["loaded"] is (model["name"] in loaded_names)
        assert model["capabilities"]
    assert body["models"][0]["context_len"] == 32768
    assert body["models"][1]["context_len"] == 8192


def test_models_openai_list_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "qwen2.5:7b", "object": "model", "created": 1700000000, "owned_by": "library"},
                    {"id": "nomic-embed-text", "object": "model", "created": 1700000001, "owned_by": "library"},
                ],
            },
        )

    client, _ = _build_client(handler)
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list) and body["data"]
    for item in body["data"]:
        assert set(item) >= {"id", "object", "created", "owned_by"}
        assert item["object"] == "model"
        assert item["id"]


def test_models_owned_by_is_rewritten_to_contract_value():
    """/v1/models 是唯一改写上游 JSON 的端点：owned_by 统一为契约值。

    契约 3.2 要求 owned_by="biyou-node"，Ollama 上游返回 "library"；
    其余字段（id/object/created）必须原样透传。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"id": "m1", "object": "model", "created": 1700000000, "owned_by": "library"}],
            },
        )

    client, _ = _build_client(handler)
    item = client.get("/v1/models").json()["data"][0]
    assert item["owned_by"] == "biyou-node"
    assert item["id"] == "m1"
    assert item["object"] == "model"
    assert item["created"] == 1700000000


class _SlowSSE(httpx.AsyncByteStream):
    """慢速 SSE 上游：用于在同一个事件循环里制造真正的并发。"""

    def __init__(self, chunks: list[bytes], delay: float = 0.2) -> None:
        self._chunks = chunks
        self._delay = delay

    async def __aiter__(self):
        for chunk in self._chunks:
            await asyncio.sleep(self._delay)
            yield chunk


def _concurrent_upstream(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/tags":
        return httpx.Response(200, json=_UPSTREAM_TAGS)
    if request.url.path == "/api/ps":
        return httpx.Response(200, json=_UPSTREAM_PS)
    if request.url.path == "/api/version":
        return httpx.Response(200, json={"version": "0.5.7"})
    if request.url.path == "/v1/chat/completions":
        return httpx.Response(
            200,
            stream=_SlowSSE([b"data: one\n\n", b"data: two\n\n", b"data: [DONE]\n\n"]),
            headers={"content-type": "text/event-stream"},
        )
    return httpx.Response(404, json={"error": "unexpected path"})


def _asgi_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test-node")


def _asgi_scope(path: str) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 4010),
    }


async def _drive_asgi_stream(app, path: str, payload: dict, messages: asyncio.Queue) -> None:
    """直接驱动 ASGI，把响应消息逐条推进队列。

    不能用 httpx.ASGITransport：它会把整个响应体缓冲完才返回，等测试拿到
    流句柄时并发名额早已释放，无法观测“流进行中”的 queue_len。
    """
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}
        # 永不发 http.disconnect：让流按上游结束自然收尾
        await asyncio.Event().wait()

    async def send(message):
        await messages.put(message)

    await app(_asgi_scope(path), receive, send)


async def _next_body_chunk(messages: asyncio.Queue) -> bytes:
    msg = await asyncio.wait_for(messages.get(), timeout=5)
    assert msg["type"] == "http.response.body", msg
    return msg.get("body", b"")


def test_http_level_429_with_retry_after_and_openai_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return _concurrent_upstream(request)

    app = create_app()
    app.state.settings = Settings(
        node_id="test-node", node_name="测试节点", max_concurrency=2, mem_warn_pct=100, cpu_warn_pct=100
    )
    app.state.gate = ConcurrencyGate(2)
    app.state.client = OllamaClient("http://upstream", transport=httpx.MockTransport(handler))

    payload = {"model": "qwen2.5:7b", "messages": [{"role": "user", "content": "hi"}], "stream": True}

    async def scenario():
        async with _asgi_client(app) as client:
            responses = await asyncio.gather(
                *[client.post("/v1/chat/completions", json=payload) for _ in range(3)]
            )
            return responses

    responses = asyncio.run(scenario())
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 200, 429], statuses

    rejected = next(r for r in responses if r.status_code == 429)
    assert rejected.headers["Retry-After"] == "1"
    assert rejected.headers["X-Node-Id"] == "test-node"
    err = rejected.json()["error"]
    assert set(err) == {"message", "type", "param", "code"}
    assert err["type"] == "rate_limit_error"
    assert err["code"] == "concurrency_limit_exceeded"
    # 被拒请求不得泄漏名额；两个成功流结束后应归零
    assert app.state.gate.active == 0


def test_queue_len_reflects_in_flight_stream_and_returns_to_zero():
    def handler(request: httpx.Request) -> httpx.Response:
        return _concurrent_upstream(request)

    app = create_app()
    app.state.settings = Settings(
        node_id="test-node", node_name="测试节点", max_concurrency=1, mem_warn_pct=100, cpu_warn_pct=100
    )
    app.state.gate = ConcurrencyGate(1)
    app.state.client = OllamaClient("http://upstream", transport=httpx.MockTransport(handler))

    payload = {"model": "qwen2.5:7b", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    queue: asyncio.Queue = asyncio.Queue()
    observed: dict[str, int] = {}

    async def scenario():
        async with _asgi_client(app) as client:
            task = asyncio.create_task(
                _drive_asgi_stream(app, "/v1/chat/completions", payload, queue)
            )
            # 先等 response.start + 第一个 body chunk：此刻流仍在进行
            start = await asyncio.wait_for(queue.get(), timeout=5)
            assert start["type"] == "http.response.start"
            assert start["status"] == 200
            first = await _next_body_chunk(queue)
            assert first.startswith(b"data: ")

            health_during = await client.get("/v1/health")
            observed["during"] = health_during.json()["resources"]["queue_len"]

            while True:
                msg = await asyncio.wait_for(queue.get(), timeout=5)
                if msg["type"] == "http.response.body" and not msg.get("more_body", False):
                    break
            await task

            health_after = await client.get("/v1/health")
            observed["after"] = health_after.json()["resources"]["queue_len"]

    asyncio.run(scenario())
    assert observed["during"] == 1
    assert observed["after"] == 0
    assert app.state.gate.active == 0
