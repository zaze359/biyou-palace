from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from nodes.quasheng.config import RegistrySettings, Settings
from nodes.quasheng.registry import (
    RegistryHTTPError,
    RegistryReporter,
    models_revision,
)


class FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self.status = 200
        self.need_snapshot = False
        self.error_code = "registry_error"

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.calls.append((request.url.path, body, dict(request.headers)))
        if self.status >= 400:
            return httpx.Response(
                self.status, json={"error": {"code": self.error_code}}
            )
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(
                200, json={"ok": True, "need_snapshot": self.need_snapshot}
            )
        return httpx.Response(200, json={"ok": True, "models_rev": "sha256:x"})


def snapshot() -> dict:
    return {
        "node": {
            "id": "quasheng-test",
            "name": "测试准圣",
            "role": "quasheng",
            "arch": "x86_64",
            "runtime": "ollama",
            "runtime_version": "0.5.7",
            "contract_version": "1.0",
        },
        "status": "ready",
        "models": [
            {
                "name": "qwen2.5:7b",
                "capabilities": ["chat"],
                "context_len": 32768,
                "loaded": True,
            },
            {
                "name": "nomic-embed-text:latest",
                "capabilities": ["embedding"],
                "context_len": 8192,
                "loaded": False,
            },
        ],
        "limits": {"max_concurrency": 4, "max_context_len": 32768},
        "resources": {"queue_len": 0, "active": 0, "cpu_pct": 5.0},
    }


async def _snapshot() -> dict:
    return snapshot()


def settings(**kwargs) -> Settings:
    registry = kwargs.pop(
        "registry",
        RegistrySettings(
            enabled=True,
            base_url="http://registry.test:4020",
            heartbeat_interval_s=15,
        ),
    )
    return Settings(
        node_id="quasheng-test",
        node_name="测试准圣",
        port=4010,
        host="127.0.0.1",
        registry=registry,
        **kwargs,
    )


def test_disabled_start_is_noop(monkeypatch):
    monkeypatch.setenv("ZHENTU_REGISTRY_TOKEN", "tok")
    reporter = RegistryReporter(
        settings(registry=RegistrySettings(enabled=False)), _snapshot
    )
    assert reporter.start() is None
    assert reporter.state == "disabled"
    assert reporter.health()["enabled"] is False


def test_missing_token_is_loud_but_does_not_raise(monkeypatch):
    monkeypatch.delenv("ZHENTU_REGISTRY_TOKEN", raising=False)
    reporter = RegistryReporter(settings(), _snapshot)
    assert reporter.start() is None
    assert reporter.state == "missing_token"
    assert reporter.health()["last_error"] is None


def test_register_then_heartbeat(monkeypatch):
    monkeypatch.setenv("ZHENTU_REGISTRY_TOKEN", "tok")
    fake = FakeRegistry()
    reporter = RegistryReporter(
        settings(), _snapshot, transport=httpx.MockTransport(fake.handler)
    )
    reporter.open()
    asyncio.run(reporter.tick())
    assert fake.calls[0][0] == "/zhentu/v1/nodes/register"
    body = fake.calls[0][1]
    assert body["endpoint"] == "http://127.0.0.1:4010"
    assert body["heartbeat_interval_s"] == 15
    assert body["models"][0]["name"] == "qwen2.5:7b"
    assert reporter.state == "online"
    assert reporter.last_ok_at

    asyncio.run(reporter.tick())
    assert fake.calls[1][0] == "/zhentu/v1/nodes/quasheng-test/heartbeat"
    heartbeat = fake.calls[1][1]
    assert heartbeat["models_rev"] == models_revision(snapshot()["models"])
    assert heartbeat["loaded_models"] == ["qwen2.5:7b"]
    asyncio.run(reporter.stop())


def test_need_snapshot_forces_reregister(monkeypatch):
    monkeypatch.setenv("ZHENTU_REGISTRY_TOKEN", "tok")
    fake = FakeRegistry()
    fake.need_snapshot = True
    reporter = RegistryReporter(
        settings(), _snapshot, transport=httpx.MockTransport(fake.handler)
    )
    reporter.open()
    asyncio.run(reporter.tick())
    asyncio.run(reporter.tick())
    assert fake.calls[1][0].endswith("/heartbeat")
    asyncio.run(reporter.tick())
    assert fake.calls[2][0].endswith("/register")
    asyncio.run(reporter.stop())


def test_unknown_node_resets_registration(monkeypatch):
    monkeypatch.setenv("ZHENTU_REGISTRY_TOKEN", "tok")
    fake = FakeRegistry()
    reporter = RegistryReporter(
        settings(), _snapshot, transport=httpx.MockTransport(fake.handler)
    )
    reporter.open()
    asyncio.run(reporter.tick())
    fake.status = 404
    fake.error_code = "node_unknown"
    with pytest.raises(RegistryHTTPError) as exc:
        asyncio.run(reporter.tick())
    assert exc.value.status == 404
    assert reporter._registered is False
    asyncio.run(reporter.stop())


def test_public_endpoint_override(monkeypatch):
    monkeypatch.setenv("ZHENTU_REGISTRY_TOKEN", "tok")
    fake = FakeRegistry()
    cfg = RegistrySettings(
        enabled=True,
        base_url="http://registry.test:4020",
        heartbeat_interval_s=15,
        public_endpoint="http://10.0.0.5:4010",
    )
    reporter = RegistryReporter(
        settings(registry=cfg), _snapshot, transport=httpx.MockTransport(fake.handler)
    )
    reporter.open()
    asyncio.run(reporter.tick())
    assert fake.calls[0][1]["endpoint"] == "http://10.0.0.5:4010"
    asyncio.run(reporter.stop())


def test_models_revision_ignores_loaded_and_order():
    first = snapshot()["models"]
    second = list(reversed(first))
    second[0] = {**second[0], "loaded": not second[0]["loaded"]}
    assert models_revision(first) == models_revision(second)
    third = [dict(item) for item in first]
    third[0]["capabilities"] = ["chat", "vision"]
    assert models_revision(first) != models_revision(third)


def test_models_revision_cross_language_vector():
    models = [
        {"name": "m1", "capabilities": ["chat"], "context_len": 100},
        {"name": "m2", "capabilities": ["embedding", "chat"], "context_len": 200},
    ]
    assert (
        models_revision(models)
        == "sha256:6355eece20667e7f7642f57b2bb871c41e9a74b21f46f7b24b30d442704ee594"
    )
