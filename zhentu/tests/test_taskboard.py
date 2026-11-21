"""任务板与藏经阁的行为测试：CAS、阻塞、租约、紧凑视图与幂等。"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from zhentu.app import create_app
from zhentu.config import RegistrySettings, Secret

from .test_registry import TOKEN, Clock, auth


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
        yield client, clock


def _create(client, ticket_id="tkt-1", **overrides):
    body = {
        "ticket_id": ticket_id,
        "title": "诊断告警",
        "goal": "定位根因",
        "tier": 3,
        "risk": "qingxiu",
    }
    body.update(overrides)
    return client.post("/zhentu/v1/taskboard/tasks", json=body, headers=auth())


def _task(client, ticket_id="tkt-1"):
    return client.get(f"/zhentu/v1/taskboard/tasks/{ticket_id}", headers=auth())


def _claim(client, ticket_id="tkt-1", owner="inner-01", revision=1, ttl=300):
    return client.post(
        f"/zhentu/v1/taskboard/tasks/{ticket_id}/claim",
        json={"owner": owner, "expected_revision": revision, "lease_ttl_s": ttl},
        headers=auth(),
    )


def test_create_is_idempotent_and_compact_view(ctx):
    client, _ = ctx
    first = _create(client)
    assert first.status_code == 201
    assert first.json()["duplicate"] is False

    second = _create(client)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True

    view = _task(client).json()["task"]
    assert set(view) == {
        "ticket_id",
        "title",
        "goal",
        "tier",
        "risk",
        "status",
        "owner",
        "blocked",
        "blocked_by",
        "write_scopes",
        "revision",
        "lease",
        "updated_at",
        "created_at",
    }
    assert view["status"] == "pending"
    assert view["blocked"] is False
    assert view["lease"]["expired"] is False or view["lease"]["expires_at"] is None


def test_claim_cas_rejects_stale_revision_and_foreign_owner(ctx):
    client, _ = ctx
    _create(client)

    ok = _claim(client)
    assert ok.status_code == 200
    assert ok.json()["task"]["status"] == "in_progress"
    assert ok.json()["task"]["revision"] == 2

    stale = _claim(client)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "revision_conflict"

    foreign = _claim(client, owner="inner-02", revision=2)
    assert foreign.status_code == 409
    assert foreign.json()["error"]["code"] == "task_owned"


def test_blocked_task_cannot_claim_until_dependency_completed(ctx):
    client, _ = ctx
    _create(client, ticket_id="tkt-dep")
    _create(client, ticket_id="tkt-child", blocked_by=["tkt-dep"])

    blocked = _claim(client, ticket_id="tkt-child")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "task_blocked"

    done = client.post(
        "/zhentu/v1/taskboard/tasks/tkt-dep/update",
        json={"expected_revision": 1, "status": "completed"},
        headers=auth(),
    )
    assert done.status_code == 200

    assert _task(client, "tkt-child").json()["task"]["blocked"] is False
    assert _claim(client, ticket_id="tkt-child").status_code == 200


def test_expired_lease_can_be_reaped_and_reclaimed(ctx):
    client, clock = ctx
    _create(client)
    assert _claim(client, ttl=10).status_code == 200

    clock.advance(11)
    reap = client.post("/zhentu/v1/taskboard/reap", headers=auth())
    assert reap.status_code == 200
    assert reap.json()["released"] == ["tkt-1"]

    view = _task(client).json()["task"]
    assert view["status"] == "pending"
    assert view["owner"] is None
    assert view["revision"] == 3

    assert _claim(client, owner="inner-02", revision=3).status_code == 200


def test_results_are_append_only_and_idempotent(ctx):
    client, _ = ctx
    _create(client)

    first = client.post(
        "/zhentu/v1/taskboard/tasks/tkt-1/results",
        json={"result_id": "r1", "agent_id": "inner-01", "kind": "note", "content": "进展"},
        headers=auth(),
    )
    assert first.status_code == 201
    assert first.json()["result"]["seq"] == 1

    duplicate = client.post(
        "/zhentu/v1/taskboard/tasks/tkt-1/results",
        json={"result_id": "r1", "agent_id": "inner-01", "kind": "note", "content": "进展"},
        headers=auth(),
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True

    second = client.post(
        "/zhentu/v1/taskboard/tasks/tkt-1/results",
        json={"result_id": "r2", "kind": "answer", "content": "结论"},
        headers=auth(),
    )
    assert second.status_code == 201
    assert second.json()["result"]["seq"] == 2

    listed = client.get(
        "/zhentu/v1/taskboard/tasks/tkt-1/results", headers=auth()
    ).json()
    assert [item["result_id"] for item in listed["results"]] == ["r1", "r2"]

    # 紧凑视图不携带历史与大产物
    view = _task(client).json()["task"]
    assert "results" not in view
    assert "content" not in view


def test_large_inline_result_must_use_artifact(ctx):
    client, _ = ctx
    _create(client)
    resp = client.post(
        "/zhentu/v1/taskboard/tasks/tkt-1/results",
        json={"result_id": "r-big", "kind": "artifact", "content": "x" * 8_193},
        headers=auth(),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "result_too_large"


def test_write_scope_conflict_is_only_a_hint(ctx):
    client, _ = ctx
    _create(client, ticket_id="tkt-a", write_scopes=["/var/log"])
    _create(client, ticket_id="tkt-b", write_scopes=["/var/log/app"])
    assert _claim(client, ticket_id="tkt-a").status_code == 200

    claimed = _claim(client, ticket_id="tkt-b")
    assert claimed.status_code == 200
    hints = claimed.json()["write_scope_hints"]
    assert hints == [{"ticket_id": "tkt-a", "scopes": ["/var/log/app"]}]


def test_artifact_put_is_content_addressed_and_fetchable(ctx):
    client, _ = ctx
    payload = "藏经阁产物".encode("utf-8")
    body = {
        "content_base64": base64.b64encode(payload).decode("ascii"),
        "media_type": "text/plain",
        "ticket_id": "tkt-1",
    }
    first = client.post("/zhentu/v1/taskboard/artifacts", json=body, headers=auth())
    assert first.status_code == 201
    ref = first.json()["artifact_ref"]
    assert ref.startswith("artifact://")

    second = client.post("/zhentu/v1/taskboard/artifacts", json=body, headers=auth())
    assert second.status_code == 200
    assert second.json()["duplicate"] is True

    artifact_id = first.json()["artifact_id"]
    fetched = client.get(
        f"/zhentu/v1/taskboard/artifacts/{artifact_id}", headers=auth()
    )
    assert fetched.status_code == 200
    assert fetched.content == payload
    assert fetched.headers["content-type"].startswith("text/plain")

    listed = client.get(
        "/zhentu/v1/taskboard/artifacts?ticket_id=tkt-1", headers=auth()
    ).json()
    assert listed["count"] == 1


def test_taskboard_requires_auth(ctx):
    client, _ = ctx
    assert client.get("/zhentu/v1/taskboard/tasks").status_code == 401
    assert client.get("/zhentu/v1/costs/prices").status_code == 401
