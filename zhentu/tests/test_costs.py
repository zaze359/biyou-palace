"""香火账本测试：缓存分计、幂等记账、预算阈值与告警。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from zhentu.app import create_app
from zhentu.config import RegistrySettings, Secret

from .test_registry import TOKEN, Clock, auth

PRICE_VERSION = "2026-11"


@pytest.fixture
def ctx(tmp_path):
    clock = Clock()
    settings = RegistrySettings(
        host="127.0.0.1",
        port=4020,
        db_path=str(tmp_path / "registry.db"),
        artifact_dir=str(tmp_path / "artifacts"),
        token=Secret(TOKEN),
    )
    app = create_app(settings, clock=clock)
    with TestClient(app) as client:
        client.put(
            f"/zhentu/v1/costs/prices/{PRICE_VERSION}",
            json={
                "prices": [
                    {
                        "provider": "newapi",
                        "model": "qwen2.5:7b",
                        "hit_price": 0.0001,
                        "miss_price": 0.0005,
                        "output_price": 0.001,
                        "currency": "CNY",
                    }
                ]
            },
            headers=auth(),
        )
        yield client, clock


def _usage(**overrides):
    body = {
        "entry_id": "e1",
        "ticket_id": "tkt-1",
        "step_id": "s1",
        "agent_id": "inner-01",
        "model": "qwen2.5:7b",
        "provider": "newapi",
        "price_version": PRICE_VERSION,
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_hit_tokens": 60,
        "cache_miss_tokens": 40,
    }
    body.update(overrides)
    return body


def _record(client, **overrides):
    return client.post(
        "/zhentu/v1/costs/entries", json=_usage(**overrides), headers=auth()
    )


def test_cache_hit_and_miss_priced_separately(ctx):
    client, _ = ctx
    resp = _record(client)
    assert resp.status_code == 201
    entry = resp.json()["entry"]
    # 60*0.0001 + 40*0.0005 + 10*0.001 = 0.036
    assert entry["cost"] == pytest.approx(0.036)
    assert entry["cache_savings"] == pytest.approx(0.024)
    assert entry["cache_hit_tokens"] == 60
    assert entry["cache_miss_tokens"] == 40
    assert entry["price_version"] == PRICE_VERSION


def test_cache_unknown_charges_all_input_as_miss(ctx):
    client, _ = ctx
    resp = _record(
        client, cache_unknown=True, cache_hit_tokens=None, cache_miss_tokens=None
    )
    assert resp.status_code == 201
    entry = resp.json()["entry"]
    # 100*0.0005 + 10*0.001 = 0.06
    assert entry["cost"] == pytest.approx(0.06)
    assert entry["cache_hit_tokens"] == 0
    assert entry["cache_miss_tokens"] == 100
    assert entry["cache_savings"] == 0


def test_cache_fields_are_required_or_sum_must_match(ctx):
    client, _ = ctx
    missing = _record(client, cache_hit_tokens=None, cache_miss_tokens=None)
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "cache_fields_required"

    mismatched = _record(client, cache_hit_tokens=50, cache_miss_tokens=40)
    assert mismatched.status_code == 422
    assert mismatched.json()["error"]["code"] == "invalid_usage"


def test_unknown_price_is_rejected(ctx):
    client, _ = ctx
    resp = _record(client, model="unknown-model")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "price_unknown"


def test_repeated_entry_is_not_double_charged(ctx):
    client, _ = ctx
    first = _record(client)
    assert first.status_code == 201
    second = _record(client)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True

    summary = client.get("/zhentu/v1/costs/summary/tkt-1", headers=auth()).json()
    assert summary["entries"] == 1
    assert summary["totals"]["cost"] == pytest.approx(0.036)


def test_summary_attributes_by_agent_step_and_model(ctx):
    client, _ = ctx
    assert _record(client).status_code == 201
    assert (
        _record(
            client,
            entry_id="e2",
            step_id="s2",
            agent_id="inner-02",
            input_tokens=100,
            output_tokens=0,
            cache_hit_tokens=0,
            cache_miss_tokens=100,
        ).status_code
        == 201
    )

    summary = client.get("/zhentu/v1/costs/summary/tkt-1", headers=auth()).json()
    assert summary["entries"] == 2
    assert set(summary["by_agent"]) == {"inner-01", "inner-02"}
    assert set(summary["by_step"]) == {"s1", "s2"}
    assert set(summary["by_model"]) == {"newapi/qwen2.5:7b"}
    assert summary["cache_hit_rate"] == pytest.approx(0.3)


def test_budget_warning_then_paused_and_admission_blocks(ctx):
    client, _ = ctx
    budget = {
        "max_workers": 2,
        "max_input_tokens": 10_000,
        "max_output_tokens": 10_000,
        "max_cost": 0.04,
        "max_wall_time_s": 3_600,
        "currency": "CNY",
    }
    assert (
        client.put("/zhentu/v1/costs/budgets/tkt-1", json=budget, headers=auth()).status_code
        == 200
    )

    first = _record(client)
    assert first.status_code == 201
    assert first.json()["budget"]["state"] == "warning"
    assert first.json()["budget"]["alerts"][0]["level"] == "warning"

    over = _record(client, entry_id="e2", cache_unknown=True)
    assert over.status_code == 201
    assert over.json()["budget"]["state"] == "paused"

    admission = client.post(
        "/zhentu/v1/costs/admission",
        json={"ticket_id": "tkt-1", "estimated_cost": 0.01},
        headers=auth(),
    ).json()
    assert admission["admitted"] is False
    assert admission["reason"] == "budget_exhausted"

    alerts = client.get("/zhentu/v1/costs/alerts?ticket_id=tkt-1", headers=auth()).json()
    assert [item["level"] for item in alerts["alerts"]] == ["warning", "paused"]


def test_budget_currency_must_match_price(ctx):
    client, _ = ctx
    budget = {
        "max_workers": 1,
        "max_input_tokens": 1_000,
        "max_output_tokens": 1_000,
        "max_cost": 10,
        "max_wall_time_s": 600,
        "currency": "USD",
    }
    client.put("/zhentu/v1/costs/budgets/tkt-1", json=budget, headers=auth())
    resp = _record(client)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "currency_mismatch"


def test_budget_not_found(ctx):
    client, _ = ctx
    resp = client.get("/zhentu/v1/costs/budgets/missing", headers=auth())
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "budget_unknown"
