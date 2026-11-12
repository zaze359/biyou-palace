"""回退策略（manual / auto）测试：mock 本地 Ollama 与远程上游。

覆盖：manual 候选提示不切换、auto 5xx 兜底成功并打标、4xx/401 不兜、
首字节后中断不回退、链条耗尽错误含候选与原因、启动期校验、未配置链条不变。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from nodes.quasheng import logbuffer
from nodes.quasheng.app import _validate_fallback_targets, create_app
from nodes.quasheng.concurrency import ConcurrencyGate
from nodes.quasheng.config import ConfigError, Secret, Settings, UpstreamModel, UpstreamSettings
from nodes.quasheng.ollama_client import OllamaClient
from nodes.quasheng.upstream import UpstreamRuntime

SECRET = "sk-fallback-super-secret-do-not-leak"
BASE_URL = "https://relay-a.example.com/v1"


@pytest.fixture(autouse=True)
def _reset_log_ring():
    logbuffer.clear()
    yield
    logbuffer.clear()


class _AbortSSE(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.ReadError("upstream connection reset")


def _upstream_cfg() -> UpstreamSettings:
    return UpstreamSettings(
        id="relay-a",
        base_url=BASE_URL,
        api_key=Secret(SECRET),
        api_key_env="QUASHENG_UPSTREAM_RELAY_A_KEY",
        connect_timeout_s=1.0,
        timeout_s=5.0,
        max_concurrency=2,
        visibility="both",
        data_leaves_lan=False,
        enabled=True,
        models=[UpstreamModel(id="gpt-4o-mini")],
    )


def _local_chat(model: str = "local") -> httpx.Response:
    if model:
        return httpx.Response(
            200,
            json={
                "id": "local-1",
                "object": "chat.completion",
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "local ok"}}],
            },
        )


def _make_app(
    handler,
    *,
    mode: str = "auto",
    chain: list[str] | None = None,
    upstream_cfg: UpstreamSettings | None = None,
) -> TestClient:
    cfg = upstream_cfg or _upstream_cfg()
    settings = Settings(
        node_id="fb-node",
        node_name="回退测试",
        max_concurrency=2,
        upstreams=[cfg],
        fallback_mode=mode,
        fallback_chain=list(chain or []),
    )
    app = create_app()
    app.state.settings = settings
    app.state.gate = ConcurrencyGate(2)
    app.state.client = OllamaClient(
        "http://ollama", transport=httpx.MockTransport(_local_ok)
    )
    app.state.upstreams = [UpstreamRuntime(cfg, transport=httpx.MockTransport(handler))]
    return TestClient(app, raise_server_exceptions=False)


def _local_ok(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1/chat/completions":
        payload = json.loads(request.content)
        return _local_chat(payload.get("model"))
    if path == "/api/tags":
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:0.5b"}]})
    if path == "/api/ps":
        return httpx.Response(200, json={"models": []})
    if path == "/api/version":
        return httpx.Response(200, json={"version": "0.5.7"})
    if path == "/v1/models":
        return httpx.Response(200, json={"object": "list", "data": []})
    return httpx.Response(404, json={"error": {"message": "unexpected"}})


def _upstream_handler(status: int):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "gpt-4o-mini"}]},
            )
        if request.url.path == "/v1/chat/completions":
            if status >= 400:
                return httpx.Response(
                    status,
                    json={"error": {"message": "relay failed", "code": "relay_error"}},
                )
            return httpx.Response(
                200,
                json={
                    "id": "remote-1",
                    "object": "chat.completion",
                    "model": "gpt-4o-mini",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "remote ok"}}
                    ],
                },
            )
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    return handler


_CHAT = {"model": "relay-a/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
_CHAIN = ["relay-a/gpt-4o-mini", "local/qwen2.5:0.5b"]


def _post(client: TestClient, *, stream: bool = False):
    return client.post("/v1/chat/completions", json={**_CHAT, "stream": stream})


# ---------------------------------------------------------------- manual


def test_manual_does_not_fallback_but_lists_candidates():
    local_calls: list[str] = []

    def local_handler(request: httpx.Request) -> httpx.Response:
        local_calls.append(request.url.path)
        return _local_ok(request)

    client = _make_app(_upstream_handler(502), mode="manual", chain=_CHAIN)
    client.app.state.client = OllamaClient(
        "http://ollama", transport=httpx.MockTransport(local_handler)
    )

    resp = _post(client)

    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "relay_error"
    assert body["error"]["fallback_candidates"] == ["local/qwen2.5:0.5b"]
    assert "X-Node-Fallback-Source" not in resp.headers
    assert local_calls == []


def test_manual_without_chain_is_unchanged():
    client = _make_app(_upstream_handler(502), mode="manual", chain=[])

    resp = _post(client)

    assert resp.status_code == 502
    assert "fallback_candidates" not in resp.json()["error"]


# ---------------------------------------------------------------- auto


def test_auto_falls_back_on_5xx_and_marks_source():
    client = _make_app(_upstream_handler(502), mode="auto", chain=_CHAIN)

    resp = _post(client)

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "local ok"
    assert resp.headers["X-Node-Fallback-Source"] == "local/qwen2.5:0.5b"
    entries = logbuffer.recent(20)["lines"]
    assert any(entry.get("event") == "fallback" for entry in entries)


def test_auto_does_not_fallback_on_4xx():
    local_calls: list[str] = []

    def local_handler(request: httpx.Request) -> httpx.Response:
        local_calls.append(request.url.path)
        return _local_ok(request)

    client = _make_app(_upstream_handler(400), mode="auto", chain=_CHAIN)
    client.app.state.client = OllamaClient(
        "http://ollama", transport=httpx.MockTransport(local_handler)
    )

    resp = _post(client)

    assert resp.status_code == 400
    assert "X-Node-Fallback-Source" not in resp.headers
    assert local_calls == []


def test_auto_does_not_fallback_on_401():
    client = _make_app(_upstream_handler(401), mode="auto", chain=_CHAIN)

    resp = _post(client)

    assert resp.status_code == 401
    assert "X-Node-Fallback-Source" not in resp.headers


def test_auto_exhausted_reports_attempts_and_reasons():
    local_calls: list[str] = []

    def local_handler(request: httpx.Request) -> httpx.Response:
        local_calls.append(request.url.path)
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                503, json={"error": {"message": "local down", "code": "local_down"}}
            )
        return _local_ok(request)

    client = _make_app(_upstream_handler(502), mode="auto", chain=_CHAIN)
    client.app.state.client = OllamaClient(
        "http://ollama", transport=httpx.MockTransport(local_handler)
    )

    resp = _post(client)

    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["code"] == "fallback_exhausted"
    assert [item["model"] for item in error["fallback_attempts"]] == _CHAIN
    assert error["fallback_attempts"][0]["reason"] == "relay_error"
    assert error["fallback_attempts"][1]["reason"] == "local_down"
    assert local_calls == ["/v1/chat/completions"]


def test_auto_stream_abort_after_first_byte_does_not_fallback():
    stream_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(
                200, json={"object": "list", "data": [{"id": "gpt-4o-mini"}]}
            )
        stream_calls["count"] += 1
        return httpx.Response(
            200, stream=_AbortSSE(), headers={"content-type": "text/event-stream"}
        )

    client = _make_app(handler, mode="auto", chain=_CHAIN)

    resp = _post(client, stream=True)

    assert resp.status_code == 200
    assert b"upstream_stream_aborted" in resp.content
    assert b"[DONE]" not in resp.content
    assert "X-Node-Fallback-Source" not in resp.headers
    assert stream_calls["count"] == 1  # 没有再次尝试本地候选


# ---------------------------------------------------------------- 启动校验


def _runtime(cfg: UpstreamSettings) -> UpstreamRuntime:
    return UpstreamRuntime(cfg, transport=httpx.MockTransport(_upstream_handler(200)))


def test_startup_rejects_missing_local_model():
    settings = Settings(fallback_mode="auto", fallback_chain=["local/missing:latest"])
    with pytest.raises(ConfigError) as excinfo:
        _validate_fallback_targets(settings, [], ["qwen2.5:0.5b"])
    assert "local/missing:latest" in str(excinfo.value)
    assert "本地未安装" in str(excinfo.value)


def test_startup_warns_but_accepts_missing_static_remote_model(caplog):
    """阵图侧候选不可用只降级告警，不能拦截节点启动。"""
    cfg = _upstream_cfg()
    settings = Settings(
        upstreams=[cfg], fallback_mode="auto", fallback_chain=["relay-a/not-there"]
    )
    _validate_fallback_targets(settings, [_runtime(cfg)], ["qwen2.5:0.5b"])
    assert "relay-a/not-there" in caplog.text
    assert "未通过校验" in caplog.text


def test_startup_accepts_existing_targets():
    cfg = _upstream_cfg()
    settings = Settings(
        upstreams=[cfg],
        fallback_mode="auto",
        fallback_chain=["relay-a/gpt-4o-mini", "local/qwen2.5:0.5b"],
    )
    _validate_fallback_targets(settings, [_runtime(cfg)], ["qwen2.5:0.5b"])


def test_startup_skips_remote_check_when_discovery_failed():
    cfg = _upstream_cfg()
    cfg = UpstreamSettings(
        id=cfg.id,
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        api_key_env=cfg.api_key_env,
        enabled=True,
        discover=True,
        models=[],
    )
    runtime = UpstreamRuntime(
        cfg,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                503, json={"error": {"message": "relay down"}}
            )
        ),
    )
    asyncio.run(runtime.discover())  # 首次发现失败，快照为空且无成功时间

    settings = Settings(
        upstreams=[cfg], fallback_mode="auto", fallback_chain=["relay-a/gpt-4o-mini"]
    )
    _validate_fallback_targets(settings, [runtime], ["qwen2.5:0.5b"])  # 放行并告警
