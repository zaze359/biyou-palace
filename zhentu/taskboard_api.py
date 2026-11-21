"""任务板 / 藏经阁 / 香火账本的 HTTP 路由。

挂载在 `/zhentu/v1/taskboard/*` 与 `/zhentu/v1/costs/*`；沿用阵图 Bearer 鉴权与
统一错误体。路由只做参数校验与领域错误转换，存储逻辑在 `taskboard.py`、
`artifacts.py`、`costs.py`。
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Any, Callable, Literal

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .artifacts import ArtifactStore
from .costs import CostError, CostLedger
from .taskboard import TASK_STATUSES, BoardError, TaskBoardStore

RISK_LEVELS = ("qingxiu", "rushi", "pozhen", "chuandao")


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).astimezone().isoformat()


def _budget_view(status: dict[str, Any]) -> dict[str, Any]:
    view = dict(status)
    view["started_at"] = _iso(int(view.pop("started_at_ms")))
    view["alerts"] = [
        {"level": item["level"], "created_at": _iso(int(item["created_at_ms"]))}
        for item in view.get("alerts") or []
    ]
    return view


class TaskCreateIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ticket_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=4_000)
    tier: int = Field(ge=1, le=8)
    risk: Literal["qingxiu", "rushi", "pozhen", "chuandao"]
    blocked_by: list[str] = Field(default_factory=list, max_length=64)
    write_scopes: list[str] = Field(default_factory=list, max_length=64)


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    owner: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
    lease_ttl_s: int = Field(default=300, ge=1, le=3_600)


class UpdateIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    expected_revision: int = Field(ge=1)
    status: Literal["pending", "in_progress", "completed", "deleted"] | None = None
    owner: str | None = Field(default=None, max_length=128)
    lease_ttl_s: int | None = Field(default=None, ge=1, le=3_600)


class ResultIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    result_id: str = Field(min_length=1, max_length=128)
    agent_id: str | None = Field(default=None, max_length=128)
    kind: Literal["note", "progress", "artifact", "answer"] = "note"
    content: str | None = Field(default=None, max_length=200_000)
    artifact_ref: str | None = Field(default=None, max_length=256)


class ArtifactIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content_base64: str
    media_type: str = "application/octet-stream"
    ticket_id: str | None = Field(default=None, max_length=128)
    step_id: str | None = Field(default=None, max_length=128)


class PriceIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    hit_price: float = Field(ge=0)
    miss_price: float = Field(ge=0)
    output_price: float = Field(ge=0)
    currency: str = Field(default="CNY", min_length=1, max_length=16)


class PricesIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prices: list[PriceIn] = Field(min_length=1, max_length=200)


class BudgetIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_workers: int = Field(ge=1)
    max_input_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(ge=0)
    max_cost: float = Field(ge=0)
    max_wall_time_s: int = Field(ge=1)
    currency: str | None = Field(default=None, max_length=16)


class UsageIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entry_id: str = Field(min_length=1, max_length=128)
    ticket_id: str = Field(min_length=1, max_length=128)
    step_id: str | None = Field(default=None, max_length=128)
    agent_id: str | None = Field(default=None, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    provider: str = Field(min_length=1, max_length=128)
    price_version: str = Field(min_length=1, max_length=64)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    cache_unknown: bool = False
    latency_ms: int | None = Field(default=None, ge=0)


class AdmissionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ticket_id: str = Field(min_length=1, max_length=128)
    estimated_cost: float = Field(default=0.0, ge=0)


def register_taskboard_routes(
    app: FastAPI,
    *,
    board: TaskBoardStore,
    costs: CostLedger,
    artifacts: ArtifactStore,
    clock: Callable[[], int],
    require_auth: Callable[..., Any],
    fail: Callable[[int, str, str], Exception],
) -> None:
    auth = [Depends(require_auth)]

    def _guard(exc: Exception) -> None:
        if isinstance(exc, (BoardError, CostError)):
            raise fail(exc.status, exc.code, exc.message) from exc
        raise exc

    # ---------------------------------------------------------------- 任务板

    @app.post("/zhentu/v1/taskboard/tasks", dependencies=auth)
    async def create_board_task(request: Request, body: TaskCreateIn):
        now_ms = clock()
        try:
            task, created = board.create_task(
                ticket_id=body.ticket_id,
                title=body.title,
                goal=body.goal,
                tier=body.tier,
                risk=body.risk,
                blocked_by=body.blocked_by,
                write_scopes=body.write_scopes,
                now_ms=now_ms,
            )
        except (BoardError, CostError) as exc:
            _guard(exc)
        return JSONResponse(
            status_code=201 if created else 200,
            content={
                "task": board.compact_view(task, now_ms=now_ms),
                "duplicate": not created,
                "server_time": _iso(now_ms),
            },
        )

    @app.get("/zhentu/v1/taskboard/tasks", dependencies=auth)
    async def list_board_tasks(
        request: Request,
        status: str | None = None,
        owner: str | None = None,
        limit: int = 100,
    ):
        if status and status not in TASK_STATUSES:
            raise fail(422, "invalid_status", f"status 只能是 {list(TASK_STATUSES)}")
        now_ms = clock()
        tasks = board.list_tasks(status=status, owner=owner, limit=limit)
        return {
            "server_time": _iso(now_ms),
            "count": len(tasks),
            "tasks": [board.compact_view(task, now_ms=now_ms) for task in tasks],
        }

    @app.get("/zhentu/v1/taskboard/tasks/{ticket_id}", dependencies=auth)
    async def get_board_task(ticket_id: str, request: Request):
        task = board.get_task(ticket_id)
        if task is None:
            raise fail(404, "ticket_unknown", f"任务 {ticket_id} 不存在")
        now_ms = clock()
        return {
            "server_time": _iso(now_ms),
            "task": board.compact_view(task, now_ms=now_ms),
        }

    @app.post("/zhentu/v1/taskboard/tasks/{ticket_id}/claim", dependencies=auth)
    async def claim_board_task(ticket_id: str, request: Request, body: ClaimIn):
        now_ms = clock()
        try:
            task, hints = board.claim(
                ticket_id=ticket_id,
                owner=body.owner,
                expected_revision=body.expected_revision,
                lease_ttl_s=body.lease_ttl_s,
                now_ms=now_ms,
            )
        except (BoardError, CostError) as exc:
            _guard(exc)
        return {
            "server_time": _iso(now_ms),
            "task": board.compact_view(task, now_ms=now_ms),
            "write_scope_hints": hints,
        }

    @app.post("/zhentu/v1/taskboard/tasks/{ticket_id}/update", dependencies=auth)
    async def update_board_task(ticket_id: str, request: Request, body: UpdateIn):
        now_ms = clock()
        try:
            task = board.update(
                ticket_id=ticket_id,
                expected_revision=body.expected_revision,
                status=body.status,
                owner=body.owner,
                lease_ttl_s=body.lease_ttl_s,
                now_ms=now_ms,
            )
        except (BoardError, CostError) as exc:
            _guard(exc)
        return {
            "server_time": _iso(now_ms),
            "task": board.compact_view(task, now_ms=now_ms),
        }

    @app.post("/zhentu/v1/taskboard/tasks/{ticket_id}/results", dependencies=auth)
    async def append_board_result(ticket_id: str, request: Request, body: ResultIn):
        now_ms = clock()
        try:
            result, created = board.append_result(
                ticket_id=ticket_id,
                result_id=body.result_id,
                agent_id=body.agent_id,
                kind=body.kind,
                content=body.content,
                artifact_ref=body.artifact_ref,
                now_ms=now_ms,
            )
        except (BoardError, CostError) as exc:
            _guard(exc)
        return JSONResponse(
            status_code=201 if created else 200,
            content={
                "server_time": _iso(now_ms),
                "result": result,
                "duplicate": not created,
            },
        )

    @app.get("/zhentu/v1/taskboard/tasks/{ticket_id}/results", dependencies=auth)
    async def list_board_results(
        ticket_id: str,
        request: Request,
        after_seq: int = 0,
        limit: int = 200,
    ):
        if board.get_task(ticket_id) is None:
            raise fail(404, "ticket_unknown", f"任务 {ticket_id} 不存在")
        results = board.list_results(ticket_id, after_seq=after_seq, limit=limit)
        return {
            "server_time": _iso(clock()),
            "ticket_id": ticket_id,
            "count": len(results),
            "results": results,
        }

    @app.post("/zhentu/v1/taskboard/reap", dependencies=auth)
    async def reap_board_leases(request: Request):
        now_ms = clock()
        released = board.reap_expired(now_ms)
        return {"server_time": _iso(now_ms), "released": released}

    # ---------------------------------------------------------------- 藏经阁

    @app.post("/zhentu/v1/taskboard/artifacts", dependencies=auth)
    async def put_artifact(request: Request, body: ArtifactIn):
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise fail(422, "invalid_artifact", f"content_base64 无法解码：{exc}") from exc
        now_ms = clock()
        row, created = artifacts.put(
            content,
            media_type=body.media_type,
            ticket_id=body.ticket_id,
            step_id=body.step_id,
            now_ms=now_ms,
        )
        return JSONResponse(
            status_code=201 if created else 200,
            content={
                "server_time": _iso(now_ms),
                "artifact_ref": f"artifact://{row['artifact_id']}",
                "artifact_id": row["artifact_id"],
                "size_bytes": row["size_bytes"],
                "media_type": row["media_type"],
                "duplicate": not created,
            },
        )

    @app.get("/zhentu/v1/taskboard/artifacts", dependencies=auth)
    async def list_artifacts(
        request: Request, ticket_id: str | None = None, limit: int = 100
    ):
        rows = artifacts.list(ticket_id=ticket_id, limit=limit)
        return {
            "server_time": _iso(clock()),
            "count": len(rows),
            "artifacts": [
                {
                    "artifact_ref": f"artifact://{row['artifact_id']}",
                    "artifact_id": row["artifact_id"],
                    "size_bytes": row["size_bytes"],
                    "media_type": row["media_type"],
                    "ticket_id": row["ticket_id"],
                    "step_id": row["step_id"],
                }
                for row in rows
            ],
        }

    @app.get("/zhentu/v1/taskboard/artifacts/{artifact_id}", dependencies=auth)
    async def get_artifact(artifact_id: str, request: Request):
        found = artifacts.get(artifact_id)
        if found is None:
            raise fail(404, "artifact_unknown", f"产物 {artifact_id} 不存在")
        content, media_type = found
        return Response(content=content, media_type=media_type)

    # ---------------------------------------------------------------- 香火

    @app.put("/zhentu/v1/costs/prices/{price_version}", dependencies=auth)
    async def put_prices(price_version: str, request: Request, body: PricesIn):
        try:
            count = costs.put_prices(
                price_version=price_version,
                prices=[item.model_dump() for item in body.prices],
                now_ms=clock(),
            )
        except (BoardError, CostError) as exc:
            _guard(exc)
        return {"price_version": price_version, "upserted": count}

    @app.get("/zhentu/v1/costs/prices", dependencies=auth)
    async def list_prices(request: Request, price_version: str | None = None):
        rows = costs.list_prices(price_version)
        return {"count": len(rows), "prices": rows}

    @app.put("/zhentu/v1/costs/budgets/{ticket_id}", dependencies=auth)
    async def put_budget(ticket_id: str, request: Request, body: BudgetIn):
        try:
            status = costs.set_budget(
                ticket_id=ticket_id, budget=body.model_dump(), now_ms=clock()
            )
        except (BoardError, CostError) as exc:
            _guard(exc)
        return {"ticket_id": ticket_id, "budget": _budget_view(status)}

    @app.get("/zhentu/v1/costs/budgets/{ticket_id}", dependencies=auth)
    async def get_budget(ticket_id: str, request: Request):
        status = costs.budget_status(ticket_id, now_ms=clock())
        if status is None:
            raise fail(404, "budget_unknown", f"任务 {ticket_id} 未设置预算")
        return {"ticket_id": ticket_id, "budget": _budget_view(status)}

    @app.post("/zhentu/v1/costs/entries", dependencies=auth)
    async def record_usage(request: Request, body: UsageIn):
        now_ms = clock()
        try:
            entry, created, status = costs.record_usage(
                entry=body.model_dump(), now_ms=now_ms
            )
        except (BoardError, CostError) as exc:
            _guard(exc)
        return JSONResponse(
            status_code=201 if created else 200,
            content={
                "server_time": _iso(now_ms),
                "entry": entry,
                "duplicate": not created,
                "budget": _budget_view(status) if status else None,
            },
        )

    @app.get("/zhentu/v1/costs/entries", dependencies=auth)
    async def list_cost_entries(
        request: Request,
        ticket_id: str | None = None,
        agent_id: str | None = None,
        step_id: str | None = None,
        limit: int = 500,
    ):
        rows = costs.list_entries(
            ticket_id=ticket_id, agent_id=agent_id, step_id=step_id, limit=limit
        )
        return {"count": len(rows), "entries": rows}

    @app.get("/zhentu/v1/costs/summary/{ticket_id}", dependencies=auth)
    async def cost_summary(ticket_id: str, request: Request):
        try:
            summary = costs.summary(ticket_id, now_ms=clock())
        except (BoardError, CostError) as exc:
            _guard(exc)
        if summary["budget"]:
            summary["budget"] = _budget_view(summary["budget"])
        return summary

    @app.get("/zhentu/v1/costs/alerts", dependencies=auth)
    async def list_cost_alerts(request: Request, ticket_id: str | None = None):
        rows = costs.list_alerts(ticket_id)
        return {
            "count": len(rows),
            "alerts": [
                {
                    "alert_id": row["alert_id"],
                    "ticket_id": row["ticket_id"],
                    "level": row["level"],
                    "detail": row["detail"],
                    "created_at": _iso(int(row["created_at_ms"])),
                }
                for row in rows
            ],
        }

    @app.post("/zhentu/v1/costs/admission", dependencies=auth)
    async def cost_admission(request: Request, body: AdmissionIn):
        result = costs.admission(
            ticket_id=body.ticket_id,
            estimated_cost=body.estimated_cost,
            now_ms=clock(),
        )
        if result["budget"]:
            result["budget"] = _budget_view(result["budget"])
        return result
