from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from zhentu.app import create_app
from zhentu.config import RegistrySettings, Secret
from zhentu.models import ModelEntry, models_revision

TOKEN = "test-registry-token"


class Clock:
    def __init__(self) -> None:
        self.ms = 1_700_000_000_000

    def __call__(self) -> int:
        return self.ms

    def advance(self, seconds: float) -> None:
        self.ms += int(seconds * 1000)


def auth(node_id: str = "quasheng-01", role: str = "quasheng", contract: str = "1.0"):
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-Node-Id": node_id,
        "X-Node-Role": role,
        "Contract-Version": contract,
    }


def register_body(
    *,
    node_id: str = "quasheng-01",
    role: str = "quasheng",
    status: str = "ready",
    models: list[dict[str, Any]] | None = None,
    interval: int = 15,
    boot_id: str = "boot-12345678",
    endpoint: str = "http://192.168.1.20:4010",
    queue_len: int = 0,
) -> dict[str, Any]:
    return {
        "node": {
            "id": node_id,
            "name": "准圣·01" if role == "quasheng" else "散仙·01",
            "role": role,
            "arch": "x86_64" if role == "quasheng" else "arm64",
            "runtime": "ollama" if role == "quasheng" else "llama.cpp",
            "runtime_version": "0.5.7",
            "contract_version": "1.0",
        },
        "endpoint": endpoint,
        "status": status,
        "models": models
        if models is not None
        else [
            {
                "name": "qwen2.5:7b-instruct-q4_K_M",
                "capabilities": ["chat"],
                "context_len": 32768,
                "loaded": True,
            },
            {
                "name": "nomic-embed-text:latest",
                "capabilities": ["embedding"],
                "context_len": 8192,
                "loaded": True,
            },
        ],
        "limits": {"max_concurrency": 4, "max_context_len": 32768},
        "resources": {"queue_len": queue_len, "active": 0, "cpu_pct": 12.5},
        "heartbeat_interval_s": interval,
        "boot_id": boot_id,
        "sent_at": "2026-11-14T10:00:00+08:00",
    }


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
        yield client, clock, settings


def test_health_is_open(ctx):
    client, _, _ = ctx
    resp = client.get("/zhentu/v1/health")
    assert resp.status_code == 200
    assert resp.headers["X-Registry-Version"] == "1.0"
    assert resp.json()["status"] == "ready"


def test_register_requires_auth(ctx):
    client, _, _ = ctx
    resp = client.post("/zhentu/v1/nodes/register", json=register_body())
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_register_and_query(ctx):
    client, _, _ = ctx
    resp = client.post("/zhentu/v1/nodes/register", json=register_body(), headers=auth())
    assert resp.status_code == 200
    data = resp.json()
    assert data["lease"]["ttl_s"] == 90
    assert data["models_rev"].startswith("sha256:")

    listed = client.get("/zhentu/v1/nodes", headers=auth()).json()
    assert listed["total"] == 1
    node = listed["nodes"][0]
    assert node["node"]["id"] == "quasheng-01"
    assert node["lease"]["state"] == "online"
    assert node["routable"] is True
    assert set(node["capabilities"]) == {"chat", "embedding"}
    assert node["endpoint"] == "http://192.168.1.20:4010"
    assert all(model["loaded"] for model in node["models"])


def test_embedding_alias_is_normalized(ctx):
    client, _, _ = ctx
    body = register_body(
        models=[
            {"name": "embed", "capabilities": ["embeddings"], "context_len": 512}
        ]
    )
    assert client.post(
        "/zhentu/v1/nodes/register", json=body, headers=auth()
    ).status_code == 200
    node = client.get("/zhentu/v1/nodes", headers=auth()).json()["nodes"][0]
    assert node["capabilities"] == ["embedding"]
    hit = client.get(
        "/zhentu/v1/nodes", params={"capability": "embeddings"}, headers=auth()
    ).json()
    assert hit["total"] == 1


def test_heartbeat_and_need_snapshot(ctx):
    client, clock, _ = ctx
    reg = client.post(
        "/zhentu/v1/nodes/register", json=register_body(), headers=auth()
    ).json()
    hb = client.post(
        "/zhentu/v1/nodes/quasheng-01/heartbeat",
        headers=auth(),
        json={
            "boot_id": "boot-12345678",
            "status": "degraded",
            "models_rev": reg["models_rev"],
            "loaded_models": ["qwen2.5:7b-instruct-q4_K_M"],
            "resources": {"queue_len": 2, "active": 1},
            "limits": {"max_concurrency": 4},
        },
    )
    assert hb.status_code == 200
    assert hb.json()["need_snapshot"] is False
    node = client.get("/zhentu/v1/nodes/quasheng-01", headers=auth()).json()["node"]
    assert node["status"] == "degraded"
    assert node["resources"]["queue_len"] == 2
    assert node["models"][1]["loaded"] is False

    clock.advance(10)
    changed = client.post(
        "/zhentu/v1/nodes/quasheng-01/heartbeat",
        headers=auth(),
        json={
            "boot_id": "boot-12345678",
            "status": "ready",
            "models_rev": "sha256:changed",
            "loaded_models": [],
            "resources": {},
            "limits": {},
        },
    )
    assert changed.status_code == 200
    assert changed.json()["need_snapshot"] is True


def test_heartbeat_unknown_and_boot_mismatch(ctx):
    client, _, _ = ctx
    missing = client.post(
        "/zhentu/v1/nodes/nope/heartbeat",
        headers=auth(node_id="nope"),
        json={"boot_id": "boot-12345678"},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "node_unknown"

    client.post("/zhentu/v1/nodes/register", json=register_body(), headers=auth())
    mismatch = client.post(
        "/zhentu/v1/nodes/quasheng-01/heartbeat",
        headers=auth(),
        json={"boot_id": "boot-87654321"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "boot_mismatch"


def test_lease_expiry_and_include_offline(ctx):
    client, clock, _ = ctx
    client.post("/zhentu/v1/nodes/register", json=register_body(), headers=auth())
    clock.advance(91)
    assert client.get("/zhentu/v1/nodes", headers=auth()).json()["total"] == 0
    offline = client.get(
        "/zhentu/v1/nodes", params={"include_offline": "true"}, headers=auth()
    ).json()["nodes"]
    assert offline[0]["lease"]["state"] == "offline"
    assert client.get(
        "/zhentu/v1/candidates", params={"capability": "chat"}, headers=auth()
    ).json()["candidates"] == []


def test_deregister(ctx):
    client, _, _ = ctx
    client.post("/zhentu/v1/nodes/register", json=register_body(), headers=auth())
    resp = client.post(
        "/zhentu/v1/nodes/quasheng-01/deregister",
        headers=auth(),
        json={"boot_id": "boot-12345678"},
    )
    assert resp.status_code == 200
    assert client.get("/zhentu/v1/nodes", headers=auth()).json()["total"] == 0
    detail = client.get("/zhentu/v1/nodes/quasheng-01", headers=auth()).json()["node"]
    assert detail["lease"]["state"] == "deregistered"


def test_filters_and_capabilities(ctx):
    client, _, _ = ctx
    client.post("/zhentu/v1/nodes/register", json=register_body(), headers=auth())
    sanxian = register_body(
        node_id="sanxian-01",
        role="sanxian",
        interval=30,
        endpoint="http://192.168.1.31:4011",
        models=[
            {
                "name": "Qwen2.5-VL-3B.gguf",
                "capabilities": ["chat", "vision", "ocr"],
                "context_len": 4096,
                "loaded": True,
            }
        ],
    )
    assert client.post(
        "/zhentu/v1/nodes/register",
        json=sanxian,
        headers=auth("sanxian-01", "sanxian"),
    ).status_code == 200

    assert client.get(
        "/zhentu/v1/nodes", params={"role": "sanxian"}, headers=auth()
    ).json()["total"] == 1
    assert client.get(
        "/zhentu/v1/nodes", params={"capability": "vision"}, headers=auth()
    ).json()["nodes"][0]["node"]["id"] == "sanxian-01"
    assert client.get(
        "/zhentu/v1/nodes", params={"model": "nomic-embed-text:latest"}, headers=auth()
    ).json()["total"] == 1

    caps = client.get("/zhentu/v1/capabilities", headers=auth()).json()["capabilities"]
    assert {entry["node_id"] for entry in caps["chat"]} == {"quasheng-01", "sanxian-01"}
    assert caps["embedding"][0]["model"] == "nomic-embed-text:latest"
    assert caps["vision"][0]["node_id"] == "sanxian-01"


def test_candidates_order_and_exclude(ctx):
    client, _, _ = ctx
    client.post(
        "/zhentu/v1/nodes/register",
        json=register_body(node_id="quasheng-01", queue_len=0),
        headers=auth(),
    )
    client.post(
        "/zhentu/v1/nodes/register",
        json=register_body(node_id="quasheng-02", status="degraded", queue_len=0),
        headers=auth("quasheng-02"),
    )
    candidates = client.get(
        "/zhentu/v1/candidates", params={"capability": "chat"}, headers=auth()
    ).json()["candidates"]
    assert [item["node_id"] for item in candidates] == ["quasheng-01", "quasheng-02"]

    only_second = client.get(
        "/zhentu/v1/candidates",
        params={"capability": "chat", "exclude": "quasheng-01"},
        headers=auth(),
    ).json()["candidates"]
    assert [item["node_id"] for item in only_second] == ["quasheng-02"]

    exact = client.get(
        "/zhentu/v1/candidates",
        params={"capability": "chat", "model": "qwen2.5:7b-instruct-q4_K_M"},
        headers=auth(),
    ).json()["candidates"]
    assert len(exact) == 2


def test_sanxian_ttl_floor(ctx):
    client, _, _ = ctx
    resp = client.post(
        "/zhentu/v1/nodes/register",
        json=register_body(node_id="sanxian-01", role="sanxian", interval=30),
        headers=auth("sanxian-01", "sanxian"),
    )
    assert resp.json()["lease"]["ttl_s"] == 180


def test_header_mismatch_and_endpoint_validation(ctx):
    client, _, _ = ctx
    resp = client.post(
        "/zhentu/v1/nodes/register",
        json=register_body(),
        headers=auth(node_id="another-node"),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "node_id_mismatch"

    bad = register_body(endpoint="http://user:pass@127.0.0.1:4010")
    resp = client.post("/zhentu/v1/nodes/register", json=bad, headers=auth())
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_payload"


def test_persistence_across_restart(tmp_path):
    settings = RegistrySettings(
        host="127.0.0.1",
        port=4020,
        db_path=str(tmp_path / "registry.db"),
        token=Secret(TOKEN),
    )
    clock = Clock()
    with TestClient(create_app(settings, clock=clock)) as client:
        assert client.post(
            "/zhentu/v1/nodes/register", json=register_body(), headers=auth()
        ).status_code == 200

    with TestClient(create_app(settings, clock=clock)) as client:
        rows = client.get(
            "/zhentu/v1/nodes", params={"include_offline": "true"}, headers=auth()
        ).json()
        assert rows["total"] == 1
        assert rows["nodes"][0]["node"]["id"] == "quasheng-01"


def test_invalid_payload_shape(ctx):
    client, _, _ = ctx
    body = register_body()
    body["heartbeat_interval_s"] = 5
    resp = client.post("/zhentu/v1/nodes/register", json=body, headers=auth())
    assert resp.status_code == 422
    assert "heartbeat_interval_s" in json.dumps(resp.json(), ensure_ascii=False)


def test_models_revision_cross_language_vector():
    models = [
        ModelEntry(name="m1", capabilities=["chat"], context_len=100),
        ModelEntry(name="m2", capabilities=["embedding", "chat"], context_len=200),
    ]
    # 与散仙 Kotlin NodeRegistryReporterTest 使用同一向量
    assert (
        models_revision(models)
        == "sha256:6355eece20667e7f7642f57b2bb871c41e9a74b21f46f7b24b30d442704ee594"
    )
