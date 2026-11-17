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
from nodes.quasheng.config import (
    ConfigError,
    RegistrySettings,
    Secret,
    Settings,
    UpstreamModel,
    UpstreamSettings,
)
from nodes.quasheng.ollama_client import OllamaClient
from nodes.quasheng.registry import RegistryRouteClient
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
    source: str = "static",
    registry: RegistrySettings | None = None,
    registry_transport: httpx.AsyncBaseTransport | None = None,
) -> TestClient:
    cfg = upstream_cfg or _upstream_cfg()
    settings = Settings(
        node_id="fb-node",
        node_name="回退测试",
        max_concurrency=2,
        upstreams=[cfg],
        fallback_mode=mode,
        fallback_source=source,
        fallback_chain=list(chain or []),
        registry=registry or RegistrySettings(),
    )
    app = create_app()
    app.state.settings = settings
    app.state.gate = ConcurrencyGate(2)
    app.state.client = OllamaClient(
        "http://ollama", transport=httpx.MockTransport(_local_ok)
    )
    app.state.upstreams = [UpstreamRuntime(cfg, transport=httpx.MockTransport(handler))]
    app.state.registry_transport = registry_transport
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


def _post(client: TestClient, *, stream: bool = False, model: str | None = None):
    payload = {**_CHAT, "stream": stream}
    if model:
        payload["model"] = model
    return client.post("/v1/chat/completions", json=payload)


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


# ---------------------------------------------------------------- registry 候选

_REGISTRY_ENV = "TEST_REGISTRY_TOKEN"


def _gateway_cfg(models: tuple[str, ...] = ("gpt-4o-mini", "qwen2.5:0.5b")) -> UpstreamSettings:
    return UpstreamSettings(
        id="newapi",
        base_url="http://newapi.example/v1",
        api_key=Secret(SECRET),
        api_key_env="QUASHENG_NEWAPI_KEY",
        enabled=True,
        visibility="both",
        models=[UpstreamModel(id=model) for model in models],
    )


def _registry_settings() -> RegistrySettings:
    return RegistrySettings(
        enabled=True,
        base_url="http://registry.example",
        token_env=_REGISTRY_ENV,
    )


def _registry_transport(
    candidates: list[str],
    *,
    calls: list[dict] | None = None,
    decision_id: str = "dec-test123",
    node_id: str = "quasheng-02",
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/zhentu/v1/route"
        assert request.method == "POST"
        if calls is not None:
            calls.append(json.loads(request.content))
        entries = [
            {"node_id": node_id, "model": model, "score": 0.9, "reasons": ["loaded"]}
            for model in candidates
        ]
        return httpx.Response(
            200,
            json={
                "decision_id": decision_id,
                "ttl_ms": 5000,
                "selected": entries[0] if entries else None,
                "alternates": entries[1:],
                "rejected": [],
            },
        )

    return httpx.MockTransport(handler)


def _fail_first_handler(failing: str = "gpt-4o-mini"):
    """首个模型 502，其余模型成功；用于验证候选真正被执行。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": failing}]})
        if request.url.path == "/v1/chat/completions":
            payload = json.loads(request.content)
            if payload.get("model") == failing:
                return httpx.Response(
                    502,
                    json={"error": {"message": "relay failed", "code": "relay_error"}},
                )
            return httpx.Response(
                200,
                json={
                    "id": "remote-2",
                    "object": "chat.completion",
                    "model": payload.get("model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "registry ok"},
                        }
                    ],
                },
            )
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    return handler


def test_registry_candidates_map_to_gateway_and_filter_snapshot(monkeypatch):
    monkeypatch.setenv(_REGISTRY_ENV, "registry-token")
    calls: list[dict[str, str]] = []
    client = _make_app(
        _fail_first_handler(),
        mode="auto",
        source="registry",
        registry=_registry_settings(),
        upstream_cfg=_gateway_cfg(),
        registry_transport=_registry_transport(
            ["qwen2.5:0.5b", "not-in-snapshot"], calls=calls
        ),
    )

    resp = _post(client, model="newapi/gpt-4o-mini")

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "registry ok"
    # 候选只保留 NewAPI 快照中存在的模型，并仍以 newapi/<model> 执行
    assert resp.headers["X-Node-Fallback-Source"] == "newapi/qwen2.5:0.5b"
    assert resp.headers["X-Node-Fallback-Origin"] == "registry"
    assert resp.headers["X-Node-Route-Decision"] == "dec-test123"
    entries = logbuffer.recent(20)["lines"]
    assert any(
        entry.get("event") == "fallback"
        and "候选来源 registry" in entry.get("message", "")
        and "决策 dec-test123" in entry.get("message", "")
        and "节点 quasheng-02" in entry.get("message", "")
        for entry in entries
    )
    assert calls and calls[0]["capability"] == "chat"
    assert calls[0]["exclude_nodes"] == ["fb-node"]
    assert calls[0]["max_candidates"] == 10


def test_registry_query_failure_falls_back_to_static_chain(monkeypatch):
    monkeypatch.setenv(_REGISTRY_ENV, "registry-token")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("registry down", request=request)

    client = _make_app(
        _upstream_handler(502),
        mode="auto",
        chain=["local/qwen2.5:0.5b"],
        source="registry",
        registry=_registry_settings(),
        upstream_cfg=_gateway_cfg(),
        registry_transport=httpx.MockTransport(handler),
    )

    resp = _post(client, model="newapi/gpt-4o-mini")

    assert resp.status_code == 200
    assert resp.headers["X-Node-Fallback-Source"] == "local/qwen2.5:0.5b"
    assert resp.headers["X-Node-Fallback-Origin"] == "static"
    entries = logbuffer.recent(30)["lines"]
    assert any(
        entry.get("event") == "fallback" and "阵图路由查询失败" in entry.get("message", "")
        for entry in entries
    )
    assert any(
        entry.get("event") == "fallback" and "候选来源 static" in entry.get("message", "")
        for entry in entries
    )


def test_registry_results_are_cached_between_requests(monkeypatch):
    monkeypatch.setenv(_REGISTRY_ENV, "registry-token")
    calls: list[dict[str, str]] = []
    client = _make_app(
        _upstream_handler(502),
        mode="auto",
        source="registry",
        registry=_registry_settings(),
        upstream_cfg=_gateway_cfg(),
        registry_transport=_registry_transport(["qwen2.5:0.5b"], calls=calls),
    )

    first = _post(client, model="newapi/gpt-4o-mini")
    second = _post(client, model="newapi/gpt-4o-mini")

    assert first.status_code == 502
    assert second.status_code == 502
    assert first.json()["error"]["code"] == "fallback_exhausted"
    assert first.json()["error"]["fallback_origin"] == "registry"
    # 成功决策缓存 5s：第二次请求不应再查阵图
    assert len(calls) == 1


def test_registry_candidates_all_filtered_falls_back_to_static(monkeypatch):
    monkeypatch.setenv(_REGISTRY_ENV, "registry-token")
    client = _make_app(
        _upstream_handler(502),
        mode="auto",
        chain=["local/qwen2.5:0.5b"],
        source="registry",
        registry=_registry_settings(),
        upstream_cfg=_gateway_cfg(),
        registry_transport=_registry_transport(["ghost-model"]),
    )

    resp = _post(client, model="newapi/gpt-4o-mini")

    assert resp.status_code == 200
    assert resp.headers["X-Node-Fallback-Source"] == "local/qwen2.5:0.5b"
    assert resp.headers["X-Node-Fallback-Origin"] == "static"


def test_registry_empty_without_static_chain_returns_original_failure(monkeypatch):
    monkeypatch.setenv(_REGISTRY_ENV, "registry-token")
    client = _make_app(
        _upstream_handler(502),
        mode="auto",
        source="registry",
        registry=_registry_settings(),
        upstream_cfg=_gateway_cfg(("gpt-4o-mini",)),
        registry_transport=_registry_transport([]),
    )

    resp = _post(client, model="newapi/gpt-4o-mini")

    # 没有候选时保持原始上游错误，不因阵图抖动改写错误语义
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "relay_error"
    assert "X-Node-Fallback-Origin" not in resp.headers


def test_static_source_never_queries_registry():
    calls: list[dict[str, str]] = []
    client = _make_app(
        _upstream_handler(502),
        mode="auto",
        chain=["local/qwen2.5:0.5b"],
        registry_transport=_registry_transport([], calls=calls),
    )

    resp = _post(client)

    assert resp.status_code == 200
    assert resp.headers["X-Node-Fallback-Source"] == "local/qwen2.5:0.5b"
    assert "X-Node-Fallback-Origin" not in resp.headers
    assert calls == []


def test_manual_registry_lists_filtered_candidates(monkeypatch):
    monkeypatch.setenv(_REGISTRY_ENV, "registry-token")
    client = _make_app(
        _upstream_handler(502),
        mode="manual",
        source="registry",
        registry=_registry_settings(),
        upstream_cfg=_gateway_cfg(),
        registry_transport=_registry_transport(["qwen2.5:0.5b", "not-in-snapshot"]),
    )

    resp = _post(client, model="newapi/gpt-4o-mini")

    assert resp.status_code == 502
    assert resp.json()["error"]["fallback_candidates"] == ["newapi/qwen2.5:0.5b"]
    assert "X-Node-Fallback-Source" not in resp.headers
    assert "X-Node-Fallback-Origin" not in resp.headers


def test_manual_registry_keeps_decision_order(monkeypatch):
    monkeypatch.setenv(_REGISTRY_ENV, "registry-token")
    client = _make_app(
        _upstream_handler(502),
        mode="manual",
        source="registry",
        registry=_registry_settings(),
        upstream_cfg=_gateway_cfg(("gpt-4o-mini", "qwen2.5:0.5b", "qwen2.5:7b")),
        registry_transport=_registry_transport(["qwen2.5:7b", "qwen2.5:0.5b"]),
    )

    resp = _post(client, model="newapi/gpt-4o-mini")

    # selected 在前、alternates 按阵图顺序在后
    assert resp.json()["error"]["fallback_candidates"] == [
        "newapi/qwen2.5:7b",
        "newapi/qwen2.5:0.5b",
    ]


def test_registry_route_without_decision_id_falls_back_to_static(monkeypatch):
    monkeypatch.setenv(_REGISTRY_ENV, "registry-token")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/zhentu/v1/route"
        return httpx.Response(200, json={"selected": {"model": "qwen2.5:0.5b"}})

    client = _make_app(
        _upstream_handler(502),
        mode="auto",
        chain=["local/qwen2.5:0.5b"],
        source="registry",
        registry=_registry_settings(),
        upstream_cfg=_gateway_cfg(),
        registry_transport=httpx.MockTransport(handler),
    )

    resp = _post(client, model="newapi/gpt-4o-mini")

    assert resp.status_code == 200
    assert resp.headers["X-Node-Fallback-Origin"] == "static"
    assert "X-Node-Route-Decision" not in resp.headers
    entries = logbuffer.recent(30)["lines"]
    assert any(
        entry.get("event") == "fallback" and "bad_json" in entry.get("message", "")
        for entry in entries
    )


def test_route_client_cache_respects_ttl_ms(monkeypatch):
    from types import SimpleNamespace

    from nodes.quasheng import registry as registry_module

    monkeypatch.setenv(_REGISTRY_ENV, "registry-token")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "decision_id": "dec-ttl",
                "ttl_ms": 1000,
                "selected": {"model": "m", "node_id": "n"},
                "alternates": [],
            },
        )

    settings = Settings(node_id="fb-node", registry=_registry_settings())
    client = RegistryRouteClient(settings, transport=httpx.MockTransport(handler))
    fake = {"now": 100.0}
    monkeypatch.setattr(
        registry_module, "time", SimpleNamespace(monotonic=lambda: fake["now"])
    )

    async def run():
        first = await client.decision()
        assert first.ttl_ms == 1000
        assert await client.decision() == first
        assert len(calls) == 1
        # 阵图 TTL 只有 1s：超过后必须重新决策，不能沿用 5s 默认缓存
        fake["now"] += 1.1
        assert (await client.decision()).decision_id == "dec-ttl"
        assert len(calls) == 2

    asyncio.run(run())


def test_auto_candidate_capacity_429_is_not_marked_as_fallback_success():
    """候选上游满载（429）不是回退成功：不得打回退头、写成功日志或累加成功指标。"""
    from nodes.quasheng import metrics

    metrics.reset()
    cfg = _upstream_cfg()
    cfg.max_concurrency = 1

    def local_500(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(500, json={"error": {"message": "boom", "code": "local_error"}})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:0.5b"}]})
        return httpx.Response(200, json={"models": []})

    settings = Settings(
        node_id="fb-cap",
        node_name="容量测试",
        max_concurrency=2,
        upstreams=[cfg],
        fallback_mode="auto",
        fallback_chain=["relay-a/gpt-4o-mini"],
    )
    app = create_app()
    app.state.settings = settings
    app.state.gate = ConcurrencyGate(2)
    app.state.client = OllamaClient("http://ollama", transport=httpx.MockTransport(local_500))
    app.state.upstreams = [
        UpstreamRuntime(cfg, transport=httpx.MockTransport(_upstream_handler(200)))
    ]
    client = TestClient(app, raise_server_exceptions=False)

    runtime = app.state.upstreams[0]
    assert runtime.try_acquire()  # 预占唯一名额，模拟候选上游已满载

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "qwen2.5:0.5b", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "upstream_capacity"
    assert "X-Node-Fallback-Source" not in resp.headers

    events = [l for l in logbuffer.recent(50)["lines"] if l["event"] == "fallback"]
    assert not any("已回退到" in l["message"] for l in events)
    assert "quasheng_fallback_total{" not in client.get("/metrics").text
