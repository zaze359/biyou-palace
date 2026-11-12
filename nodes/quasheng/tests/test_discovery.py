"""上游动态发现（discover=true）测试：mock 上游，不依赖网络与真实模型。

覆盖：首轮成功、白名单交集、无白名单全量、空列表、4xx/5xx、
传输失败沿用旧快照、恢复后清空错误、health 可选字段、未开启 discover 不变、
路由只认快照、失败日志不泄漏密钥与 base_url。
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

SECRET = "sk-discovery-super-secret-do-not-leak"
BASE_URL = "https://relay-a.example.com/v1"

_LOCAL_TAGS = {"models": [{"name": "llama3:8b", "details": {"family": "llama"}}]}
_LOCAL_PS = {"models": [{"name": "llama3:8b"}]}


@pytest.fixture(autouse=True)
def _reset_log_ring():
    logbuffer.clear()
    yield
    logbuffer.clear()


def _cfg(
    *,
    models: list[UpstreamModel] | None = None,
    discover: bool = True,
    visibility: str = "both",
    interval: float = 60.0,
) -> UpstreamSettings:
    return UpstreamSettings(
        id="relay-a",
        base_url=BASE_URL,
        api_key=Secret(SECRET),
        api_key_env="QUASHENG_UPSTREAM_RELAY_A_KEY",
        connect_timeout_s=1.0,
        timeout_s=5.0,
        max_concurrency=2,
        visibility=visibility,
        data_leaves_lan=False,
        enabled=True,
        discover=discover,
        discover_interval_s=interval,
        models=list(models or []),
    )


def _models_handler(
    ids: list[str] | None = None,
    *,
    status: int = 200,
    bad_payload: bool = False,
):
    payload = {"object": "list"} if bad_payload else {
        "object": "list",
        "data": [{"id": model_id, "object": "model"} for model_id in (ids or [])],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    return handler


def _run_discover(cfg: UpstreamSettings, handler) -> tuple[bool, list[str], str | None, str | None]:
    """执行一次发现并返回 (成功, 模型 id, 最近成功时间, 错误码)。"""

    async def run():
        runtime = UpstreamRuntime(cfg, transport=httpx.MockTransport(handler))
        try:
            ok = await runtime.discover()
            return (
                ok,
                [model.id for model in runtime.models],
                runtime.last_discovery_at,
                runtime.discovery_error,
            )
        finally:
            await runtime.aclose()

    return asyncio.run(run())


# ---------------------------------------------------------------- 发现语义


def test_first_discovery_intersects_whitelist():
    cfg = _cfg(
        models=[
            UpstreamModel(id="m1", capabilities=["chat"], context_len=1024),
            UpstreamModel(id="m2", capabilities=["embedding"], context_len=2048),
        ]
    )

    ok, ids, discovered_at, error = _run_discover(cfg, _models_handler(["m2", "m3"]))

    assert ok is True
    assert ids == ["m2"]  # 白名单顺序，且 m3 不在白名单、m1 未被上游返回
    assert discovered_at is not None
    assert error is None


def test_discovery_without_whitelist_exposes_all_with_chat_default():
    cfg = _cfg(models=[])

    ok, ids, discovered_at, error = _run_discover(cfg, _models_handler(["zeta", "alpha", "zeta"]))

    assert ok is True
    assert ids == ["zeta", "alpha"]  # 去重且保持上游顺序
    assert discovered_at is not None
    assert error is None


def test_discovery_empty_list_is_successful_snapshot():
    cfg = _cfg(models=[UpstreamModel(id="m1")])

    ok, ids, discovered_at, error = _run_discover(cfg, _models_handler([]))

    assert ok is True
    assert ids == []
    assert discovered_at is not None
    assert error is None


def test_discovery_failure_keeps_previous_snapshot_http():
    cfg = _cfg(models=[UpstreamModel(id="m1")])
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(200, json={"object": "list", "data": [{"id": "m1"}]})
        return httpx.Response(500, json={"error": {"message": "boom"}})

    async def run():
        runtime = UpstreamRuntime(cfg, transport=httpx.MockTransport(handler))
        try:
            await runtime.discover()
            first_at = runtime.last_discovery_at
            ok = await runtime.discover()
            return ok, [m.id for m in runtime.models], runtime.last_discovery_at, runtime.discovery_error, first_at
        finally:
            await runtime.aclose()

    ok, ids, discovered_at, error, first_at = asyncio.run(run())

    assert ok is False
    assert ids == ["m1"]  # 失败不清空
    assert discovered_at == first_at  # 快照时间保持上一次成功
    assert error == "http_500"


def test_discovery_transport_error_keeps_previous_snapshot():
    cfg = _cfg(models=[UpstreamModel(id="m1")])
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(200, json={"object": "list", "data": [{"id": "m1"}]})
        raise httpx.ConnectError("refused", request=request)

    async def run():
        runtime = UpstreamRuntime(cfg, transport=httpx.MockTransport(handler))
        try:
            await runtime.discover()
            ok = await runtime.discover()
            return ok, [m.id for m in runtime.models], runtime.discovery_error
        finally:
            await runtime.aclose()

    ok, ids, error = asyncio.run(run())

    assert ok is False
    assert ids == ["m1"]
    assert error == "upstream_unavailable"


def test_discovery_bad_payload_marks_error_and_keeps_snapshot():
    cfg = _cfg(models=[UpstreamModel(id="m1")])
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(200, json={"object": "list", "data": [{"id": "m1"}]})
        return httpx.Response(200, json={"object": "list"})

    async def run():
        runtime = UpstreamRuntime(cfg, transport=httpx.MockTransport(handler))
        try:
            await runtime.discover()
            ok = await runtime.discover()
            return ok, [m.id for m in runtime.models], runtime.discovery_error
        finally:
            await runtime.aclose()

    ok, ids, error = asyncio.run(run())

    assert ok is False
    assert ids == ["m1"]
    assert error == "bad_response"


def test_discovery_recovers_and_clears_error():
    cfg = _cfg(models=[])
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, json={"object": "list", "data": [{"id": "recovered"}]})

    async def run():
        runtime = UpstreamRuntime(cfg, transport=httpx.MockTransport(handler))
        try:
            first = await runtime.discover()
            first_error = runtime.discovery_error
            second = await runtime.discover()
            return (
                first,
                first_error,
                second,
                [m.id for m in runtime.models],
                runtime.discovery_error,
                runtime.last_discovery_at,
            )
        finally:
            await runtime.aclose()

    first, first_error, second, ids, error, discovered_at = asyncio.run(run())

    assert first is False and first_error == "http_503"
    assert second is True
    assert ids == ["recovered"]
    assert error is None
    assert discovered_at is not None


def test_discovery_failure_does_not_leak_secret_or_base_url(caplog):
    caplog.set_level(logging.INFO)
    cfg = _cfg(models=[UpstreamModel(id="m1")])

    ok, _, _, error = _run_discover(cfg, _models_handler(status=500))

    assert ok is False and error == "http_500"
    assert "模型发现失败" in caplog.text
    assert SECRET not in caplog.text
    assert BASE_URL not in caplog.text
    dump = json.dumps(logbuffer.recent(50), ensure_ascii=False)
    assert SECRET not in dump
    assert BASE_URL not in dump


# ---------------------------------------------------------------- HTTP 表面


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
            json={"object": "list", "data": [{"id": "llama3:8b", "object": "model"}]},
        )
    if path == "/v1/chat/completions":
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "up-1",
                "object": "chat.completion",
                "model": payload.get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            },
        )
    return httpx.Response(404, json={"error": {"message": "unexpected"}})


def _upstream_ok(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/models":
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "m2", "object": "model"}]},
        )
    if request.url.path == "/v1/chat/completions":
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "up-1",
                "object": "chat.completion",
                "model": payload.get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "remote ok"}}],
            },
        )
    return httpx.Response(404, json={"error": {"message": "unexpected"}})


def _make_app(cfg: UpstreamSettings, upstream_handler=_upstream_ok):
    settings = Settings(
        node_id="test-node", node_name="测试节点", max_concurrency=2, upstreams=[cfg]
    )
    app = create_app()
    app.state.settings = settings
    app.state.gate = ConcurrencyGate(2)
    app.state.client = OllamaClient("http://ollama", transport=httpx.MockTransport(_local_ok))
    runtime = UpstreamRuntime(cfg, transport=httpx.MockTransport(upstream_handler))
    app.state.upstreams = [runtime]
    return TestClient(app, raise_server_exceptions=False), runtime


def test_health_reports_discovery_fields():
    cfg = _cfg(models=[])
    app_client, runtime = _make_app(cfg)
    asyncio.run(_discover_only(runtime))

    body = app_client.get("/v1/health").json()
    state = next(item for item in body["upstreams"] if item["id"] == "relay-a")
    assert state["discover"] is True
    assert state["last_discovery_at"] is not None
    assert state["discovery_error"] is None


def test_health_omits_discovery_fields_when_disabled():
    cfg = _cfg(discover=False, models=[UpstreamModel(id="m1")])
    app_client, _ = _make_app(cfg)

    body = app_client.get("/v1/health").json()
    state = next(item for item in body["upstreams"] if item["id"] == "relay-a")
    assert "discover" not in state
    assert "last_discovery_at" not in state
    assert "discovery_error" not in state


async def _discover_only(runtime: UpstreamRuntime) -> None:
    await runtime.discover()


def test_models_endpoint_reflects_latest_snapshot():
    cfg = _cfg(models=[])
    state = {"ids": ["m2"]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"id": model_id, "object": "model"} for model_id in state["ids"]],
            },
        )

    app_client, runtime = _make_app(cfg, handler)
    asyncio.run(_discover_only(runtime))

    ids = [item["id"] for item in app_client.get("/v1/models").json()["data"]]
    assert "relay-a/m2" in ids

    # 第二次发现只剩 m3：快照被替换，m2 不再对外
    state["ids"] = ["m3"]
    asyncio.run(_discover_only(runtime))

    ids = [item["id"] for item in app_client.get("/v1/models").json()["data"]]
    assert "relay-a/m3" in ids
    assert "relay-a/m2" not in ids


def test_chat_routes_only_snapshot_models():
    cfg = _cfg(
        models=[UpstreamModel(id="m1"), UpstreamModel(id="m2")],
    )
    app_client, runtime = _make_app(cfg)
    asyncio.run(_discover_only(runtime))  # 上游只返回 m2

    missing = app_client.post(
        "/v1/chat/completions",
        json={"model": "relay-a/m1", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "model_not_found"

    found = app_client.post(
        "/v1/chat/completions",
        json={"model": "relay-a/m2", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert found.status_code == 200
    assert found.json()["model"] == "m2"  # 转发时换回上游原生 id


def test_discovery_disabled_keeps_static_behavior():
    cfg = _cfg(discover=False, models=[UpstreamModel(id="m1")])
    app_client, runtime = _make_app(cfg)

    assert [model.id for model in runtime.models] == ["m1"]
    ids = [item["id"] for item in app_client.get("/v1/models").json()["data"]]
    assert "relay-a/m1" in ids

    ok = app_client.post(
        "/v1/chat/completions",
        json={"model": "relay-a/m1", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert ok.status_code == 200
