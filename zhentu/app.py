"""阵图 ZhenTu · 节点注册表 HTTP 服务。

端点前缀 /zhentu/v1，与 NewAPI 的 /api、/v1 完全隔离。
健康检查免鉴权；其余端点统一 Bearer。
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .aggregator import Aggregator, ConcatAggregator, LlmAggregator
from .config import REGISTRY_VERSION, ConfigError, RegistrySettings, load_settings
from .ledger import TaskStore
from .metrics import RouteMetrics
from .models import (
    CANONICAL_CAPABILITIES,
    DeregisterRequest,
    HeartbeatRequest,
    NodeStatus,
    RegisterRequest,
    RouteRequest,
    capability_union,
    models_revision,
    normalize_capability,
    ttl_for,
)
from .orchestration import (
    Executor,
    NewApiExecutor,
    Planner,
    PlannerClient,
    SingleStepPlanner,
)
from .route import MAX_ALLOWED_MODELS, STRATEGIES, RouteQuery, bare_model, decide
from .runner import TaskRunner
from .store import RegistryStore
from .task_models import TaskCreateRequest

logger = logging.getLogger("zhentu")

_MAX_LIMIT = 500
_NODE_STATUSES = {"ready", "loading", "degraded", "down"}
_ROLES = {"quasheng", "sanxian"}


class RegistryError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        type_: str = "invalid_request_error",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.type = type_


def _error_response(
    status: int,
    code: str,
    message: str,
    *,
    type_: str = "invalid_request_error",
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {"message": message, "type": type_, "param": None, "code": code}
        },
        headers={"X-Registry-Version": REGISTRY_VERSION},
    )


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).astimezone().isoformat()


def _lease(row: dict[str, Any], now_ms: int) -> dict[str, Any]:
    last_seen = int(row["last_seen_ms"])
    ttl_s = int(row["ttl_s"])
    if row.get("deregistered_at_ms"):
        state = "deregistered"
    elif now_ms - last_seen <= ttl_s * 1000:
        state = "online"
    else:
        state = "offline"
    return {
        "state": state,
        "last_seen_at": _iso(last_seen),
        "age_s": max(0, (now_ms - last_seen) // 1000),
        "ttl_s": ttl_s,
        "expires_at": _iso(last_seen + ttl_s * 1000),
    }


def _node_view(row: dict[str, Any], now_ms: int) -> dict[str, Any]:
    lease = _lease(row, now_ms)
    loaded = set(row.get("loaded_json") or [])
    models = [{**model, "loaded": model["name"] in loaded} for model in row["models_json"]]
    payload = row.get("payload_json") or {}
    node = payload.get("node") or {
        "id": row["node_id"],
        "name": row["name"],
        "role": row["role"],
        "contract_version": row["contract_version"],
    }
    return {
        "node": node,
        "endpoint": row["endpoint"],
        "status": row["status"],
        "lease": lease,
        "models": models,
        "capabilities": row["capabilities_json"],
        "limits": row["limits_json"],
        "resources": row["resources_json"],
        "boot_id": row["boot_id"],
        "revision": row["revision"],
        "routable": lease["state"] == "online" and row["status"] in ("ready", "degraded"),
    }


def _matches(
    view: dict[str, Any],
    *,
    role: str | None,
    status: str | None,
    capability: str | None,
    model: str | None,
) -> bool:
    if role and view["node"].get("role") != role:
        return False
    if status and view["status"] != status:
        return False
    if capability and capability not in view["capabilities"]:
        return False
    if model and model not in [entry["name"] for entry in view["models"]]:
        return False
    return True


def _view_sort_key(view: dict[str, Any]) -> tuple:
    rank = 0 if view["status"] == "ready" else 1 if view["status"] == "degraded" else 2
    queue_len = int((view["resources"] or {}).get("queue_len") or 0)
    return (
        0 if view["lease"]["state"] == "online" else 1,
        rank,
        queue_len,
        view["node"].get("id") or "",
    )


def create_app(
    settings: RegistrySettings | None = None,
    clock: Callable[[], int] | None = None,
    *,
    planner: Planner | None = None,
    executor: Executor | None = None,
    aggregator: Aggregator | None = None,
) -> FastAPI:
    cfg = settings or load_settings()
    cfg.validate()
    store = RegistryStore(cfg.db_path)
    ledger = TaskStore(cfg.db_path)
    now = clock or (lambda: int(time.time() * 1000))

    selected_planner: Planner = planner or PlannerClient(
        base_url=cfg.newapi_base_url,
        token=cfg.newapi_token,
        model=cfg.planner_model,
        timeout_s=min(30.0, cfg.newapi_timeout_s),
        fallback=SingleStepPlanner(),
    )
    selected_executor: Executor = executor or NewApiExecutor(
        base_url=cfg.newapi_base_url,
        token=cfg.newapi_token,
        timeout_s=cfg.newapi_timeout_s,
    )
    if aggregator is not None:
        selected_aggregator: Aggregator = aggregator
    elif cfg.aggregator_mode == "llm" and cfg.aggregator_model:
        selected_aggregator = LlmAggregator(
            base_url=cfg.newapi_base_url,
            token=cfg.newapi_token,
            model=cfg.aggregator_model,
            timeout_s=cfg.aggregator_timeout_s,
            fallback=ConcatAggregator(),
        )
    else:
        selected_aggregator = ConcatAggregator()

    def route_step(step: dict[str, Any]) -> dict[str, Any] | None:
        now_ms = now()
        query = RouteQuery(
            capability=step.get("capability") or "chat",
            model=step.get("model_hint"),
            context_len=None,
            strategy="balanced",
            max_candidates=3,
        )
        rows = list(store.list_all())
        decision = decide(((row, _node_view(row, now_ms)) for row in rows), query)
        return {
            "request": {
                "capability": query.capability,
                "model": query.model,
                "strategy": query.strategy,
            },
            "selected": decision.selected,
            "rejected": decision.rejected,
        }

    runner = TaskRunner(
        ledger=ledger,
        planner=selected_planner,
        executor=selected_executor,
        router=route_step,
        clock=now,
        aggregator=selected_aggregator,
        max_events=cfg.task_event_max_per_task,
        retention_ms=cfg.task_event_retention_days * 86_400_000,
        step_timeout_s=cfg.newapi_timeout_s,
        aggregator_timeout_s=cfg.aggregator_timeout_s,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.started_ms = now()
        try:
            yield
        finally:
            tasks = list(app.state.background_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            ledger.close()
            store.close()

    app = FastAPI(
        title="BiYouPalace 阵图 · 节点注册表",
        version=REGISTRY_VERSION,
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.store = store
    app.state.ledger = ledger
    app.state.clock = now
    app.state.route_metrics = RouteMetrics()
    app.state.planner = selected_planner
    app.state.executor = selected_executor
    app.state.aggregator = selected_aggregator
    app.state.runner = runner
    app.state.background_tasks = set()

    async def require_auth(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        token = request.app.state.settings.token
        if not token:
            return
        scheme, _, value = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(value, token.get()):
            raise RegistryError(401, "unauthorized", "缺少或无效的 bearer token")

    @app.middleware("http")
    async def registry_headers(request: Request, call_next):
        length = request.headers.get("content-length")
        if length:
            try:
                too_large = int(length) > request.app.state.settings.max_body_bytes
            except ValueError:
                too_large = True
            if too_large:
                return _error_response(413, "payload_too_large", "请求体超过大小限制")
        response = await call_next(request)
        response.headers["X-Registry-Version"] = REGISTRY_VERSION
        return response

    @app.exception_handler(RegistryError)
    async def handle_registry_error(request: Request, exc: RegistryError):
        return _error_response(exc.status, exc.code, exc.message, type_=exc.type)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        first = (exc.errors() or [{}])[0]
        message = str(first.get("msg") or "请求体格式错误")
        return _error_response(422, "invalid_payload", message)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        type_ = "invalid_request_error" if exc.status_code < 500 else "server_error"
        return _error_response(exc.status_code, code, str(exc.detail), type_=type_)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        return _error_response(500, "internal_error", "注册表内部错误", type_="server_error")

    # ---------------------------------------------------------------- 健康

    @app.get("/zhentu/v1/health")
    async def health(request: Request):
        db_ok = request.app.state.store.ping()
        total, online = request.app.state.store.counts(request.app.state.clock())
        return {
            "status": "ready" if db_ok else "degraded",
            "registry_version": REGISTRY_VERSION,
            "server_time": _iso(request.app.state.clock()),
            "db": "ok" if db_ok else "error",
            "nodes_total": total,
            "nodes_online": online,
            "uptime_s": max(0, (request.app.state.clock() - app.state.started_ms) // 1000),
        }

    # ---------------------------------------------------------------- 注册

    @app.post(
        "/zhentu/v1/nodes/register",
        dependencies=[Depends(require_auth)],
    )
    async def register(
        request: Request,
        body: RegisterRequest,
        x_node_id: str | None = Header(default=None, alias="X-Node-Id"),
        x_node_role: str | None = Header(default=None, alias="X-Node-Role"),
        contract_version: str | None = Header(default=None, alias="Contract-Version"),
    ):
        if x_node_id and x_node_id != body.node.id:
            raise RegistryError(400, "node_id_mismatch", "X-Node-Id 与 body.node.id 不一致")
        if x_node_role and x_node_role != body.node.role:
            raise RegistryError(400, "node_role_mismatch", "X-Node-Role 与 body.node.role 不一致")
        if contract_version and contract_version != body.node.contract_version:
            raise RegistryError(
                400, "contract_version_mismatch", "Contract-Version 与 body.node.contract_version 不一致"
            )

        models = [model.model_dump() for model in body.models]
        capabilities = capability_union(body.models)
        revision = models_revision(body.models)
        ttl_s = ttl_for(body.node.role, body.heartbeat_interval_s, cfg.ttl_max_s)
        now_ms = request.app.state.clock()
        row = store.register(
            node=body.node.model_dump(),
            endpoint=body.endpoint,
            status=body.status,
            models=models,
            limits=body.limits,
            resources=body.resources,
            heartbeat_interval_s=body.heartbeat_interval_s,
            ttl_s=ttl_s,
            boot_id=body.boot_id,
            models_rev=revision,
            capabilities=capabilities,
            sent_at=body.sent_at,
            now_ms=now_ms,
        )
        return {
            "ok": True,
            "node_id": row["node_id"],
            "revision": row["revision"],
            "models_rev": row["models_rev"],
            "lease": {
                "ttl_s": row["ttl_s"],
                "expires_at": _iso(row["last_seen_ms"] + row["ttl_s"] * 1000),
            },
            "server_time": _iso(now_ms),
            "registry_version": REGISTRY_VERSION,
        }

    @app.post(
        "/zhentu/v1/nodes/{node_id}/heartbeat",
        dependencies=[Depends(require_auth)],
    )
    async def heartbeat(node_id: str, request: Request, body: HeartbeatRequest):
        now_ms = request.app.state.clock()
        result, row, need_snapshot = store.heartbeat(
            node_id=node_id,
            boot_id=body.boot_id,
            status=body.status,
            models_rev=body.models_rev,
            loaded_models=body.loaded_models,
            resources=body.resources,
            limits=body.limits,
            now_ms=now_ms,
        )
        if result == "unknown":
            raise RegistryError(404, "node_unknown", f"节点 {node_id} 未注册")
        if result == "boot_mismatch":
            raise RegistryError(409, "boot_mismatch", "boot_id 与当前注册不一致，请重新注册")
        assert row is not None
        return {
            "ok": True,
            "node_id": node_id,
            "need_snapshot": need_snapshot,
            "lease": {
                "ttl_s": row["ttl_s"],
                "expires_at": _iso(row["last_seen_ms"] + row["ttl_s"] * 1000),
            },
            "server_time": _iso(now_ms),
            "registry_version": REGISTRY_VERSION,
        }

    @app.post(
        "/zhentu/v1/nodes/{node_id}/deregister",
        dependencies=[Depends(require_auth)],
    )
    async def deregister(node_id: str, request: Request, body: DeregisterRequest):
        now_ms = request.app.state.clock()
        if not store.deregister(node_id, body.boot_id, now_ms):
            raise RegistryError(404, "node_unknown", f"节点 {node_id} 未注册或 boot_id 不一致")
        return {
            "ok": True,
            "node_id": node_id,
            "state": "deregistered",
            "server_time": _iso(now_ms),
            "registry_version": REGISTRY_VERSION,
        }

    # ---------------------------------------------------------------- 查询

    @app.get("/zhentu/v1/nodes", dependencies=[Depends(require_auth)])
    async def list_nodes(
        request: Request,
        include_offline: bool = False,
        role: str | None = None,
        status: str | None = None,
        capability: str | None = None,
        model: str | None = None,
        limit: int = 100,
    ):
        if role and role not in _ROLES:
            raise RegistryError(400, "invalid_role", f"role 只能是 {sorted(_ROLES)}")
        if status and status not in _NODE_STATUSES:
            raise RegistryError(400, "invalid_status", f"status 只能是 {sorted(_NODE_STATUSES)}")
        if capability:
            try:
                capability = normalize_capability(capability)
            except ValueError as exc:
                raise RegistryError(400, "invalid_capability", str(exc)) from None
        now_ms = request.app.state.clock()
        views = []
        for row in store.list_all():
            view = _node_view(row, now_ms)
            if not include_offline and view["lease"]["state"] != "online":
                continue
            if _matches(
                view, role=role, status=status, capability=capability, model=model
            ):
                views.append(view)
        views.sort(key=_view_sort_key)
        capped = max(1, min(limit, _MAX_LIMIT))
        return {
            "registry_version": REGISTRY_VERSION,
            "server_time": _iso(now_ms),
            "total": len(views),
            "nodes": views[:capped],
        }

    @app.get("/zhentu/v1/nodes/{node_id}", dependencies=[Depends(require_auth)])
    async def get_node(node_id: str, request: Request):
        row = store.get(node_id)
        if row is None:
            raise RegistryError(404, "node_unknown", f"节点 {node_id} 未注册")
        return {
            "registry_version": REGISTRY_VERSION,
            "server_time": _iso(request.app.state.clock()),
            "node": _node_view(row, request.app.state.clock()),
        }

    @app.get("/zhentu/v1/capabilities", dependencies=[Depends(require_auth)])
    async def capabilities(request: Request, include_offline: bool = False):
        now_ms = request.app.state.clock()
        result: dict[str, list[dict[str, Any]]] = {cap: [] for cap in CANONICAL_CAPABILITIES}
        for row in store.list_all():
            view = _node_view(row, now_ms)
            if not include_offline and view["lease"]["state"] != "online":
                continue
            resources = view["resources"] or {}
            limits = view["limits"] or {}
            for entry in view["models"]:
                for cap in entry["capabilities"]:
                    result.setdefault(cap, []).append(
                        {
                            "node_id": view["node"].get("id"),
                            "node_name": view["node"].get("name"),
                            "role": view["node"].get("role"),
                            "model": entry["name"],
                            "loaded": entry["loaded"],
                            "status": view["status"],
                            "queue_len": int(resources.get("queue_len") or 0),
                            "max_concurrency": limits.get("max_concurrency"),
                            "routable": view["routable"],
                        }
                    )
        for entries in result.values():
            entries.sort(key=lambda item: (item["node_id"] or "", item["model"]))
        return {
            "registry_version": REGISTRY_VERSION,
            "server_time": _iso(now_ms),
            "capabilities": result,
        }

    @app.get("/zhentu/v1/candidates", dependencies=[Depends(require_auth)])
    async def candidates(
        request: Request,
        capability: str = "chat",
        model: str | None = None,
        exclude: str | None = None,
        limit: int = 5,
        include_loading: bool = False,
        include_degraded: bool = True,
    ):
        try:
            capability = normalize_capability(capability)
        except ValueError as exc:
            raise RegistryError(400, "invalid_capability", str(exc)) from None
        excluded = {item.strip() for item in (exclude or "").split(",") if item.strip()}
        now_ms = request.app.state.clock()
        scored: list[tuple[tuple, dict[str, Any]]] = []
        for row in store.list_all():
            view = _node_view(row, now_ms)
            if view["lease"]["state"] != "online" or not view["routable"]:
                continue
            if view["status"] == "loading" and not include_loading:
                continue
            if view["status"] == "degraded" and not include_degraded:
                continue
            node_id = view["node"].get("id")
            if node_id in excluded:
                continue
            resources = view["resources"] or {}
            limits = view["limits"] or {}
            queue_len = int(resources.get("queue_len") or 0)
            max_concurrency = int(limits.get("max_concurrency") or 0)
            effective_max_concurrency = int(
                limits.get("effective_max_concurrency") or 0
            )
            if effective_max_concurrency <= 0:
                effective_max_concurrency = max_concurrency
            active = int(resources.get("active") or 0)
            ratio = (
                active / effective_max_concurrency
                if effective_max_concurrency > 0
                else 0.0
            )
            for entry in view["models"]:
                if capability not in entry["capabilities"]:
                    continue
                if model and entry["name"] != model:
                    continue
                rank = 0 if view["status"] == "ready" else 1
                score = (rank, queue_len, ratio, -int(row["last_seen_ms"]), node_id or "", entry["name"])
                scored.append(
                    (
                        score,
                        {
                            "node_id": node_id,
                            "node_name": view["node"].get("name"),
                            "role": view["node"].get("role"),
                            "endpoint": view["endpoint"],
                            "model": entry["name"],
                            "loaded": entry["loaded"],
                            "status": view["status"],
                            "queue_len": queue_len,
                            "max_concurrency": max_concurrency,
                            "effective_max_concurrency": effective_max_concurrency,
                            "active": active,
                        },
                    )
                )
        scored.sort(key=lambda item: item[0])
        capped = max(1, min(limit, 50))
        return {
            "registry_version": REGISTRY_VERSION,
            "server_time": _iso(now_ms),
            "capability": capability,
            "candidates": [item for _, item in scored[:capped]],
        }

    # ---------------------------------------------------------------- 编排

    @app.post("/zhentu/v1/route", dependencies=[Depends(require_auth)])
    async def route_decision(request: Request, body: RouteRequest):
        """单跳选路：只返回决策，不代理请求、不持有任何凭据。"""
        try:
            capability = normalize_capability(body.capability)
        except ValueError as exc:
            raise RegistryError(400, "invalid_capability", str(exc)) from None
        if body.strategy not in STRATEGIES:
            raise RegistryError(
                400, "invalid_strategy", f"strategy 只能是 {list(STRATEGIES)}"
            )
        if body.allowed_models is not None and len(body.allowed_models) > MAX_ALLOWED_MODELS:
            raise RegistryError(
                400,
                "too_many_models",
                f"allowed_models 最多 {MAX_ALLOWED_MODELS} 项",
            )

        query = RouteQuery(
            capability=capability,
            model=body.model,
            context_len=body.context_len,
            strategy=body.strategy,
            max_candidates=body.max_candidates,
            exclude_nodes=frozenset(
                item.strip() for item in body.exclude_nodes if item.strip()
            ),
            exclude_models=frozenset(
                bare_model(item) for item in body.exclude_models if item.strip()
            ),
            allowed_models=(
                frozenset(bare_model(item) for item in body.allowed_models if item.strip())
                if body.allowed_models
                else None
            ),
            allow_loading=body.allow_loading,
            allow_shedding=body.allow_shedding,
        )

        now_ms = request.app.state.clock()
        try:
            rows = list(request.app.state.store.list_all())
        except sqlite3.Error as exc:
            raise RegistryError(
                503,
                "registry_unavailable",
                "注册表存储不可用，暂无法给出路由决策",
                type_="server_error",
            ) from exc
        started = time.perf_counter()
        decision = decide(
            ((row, _node_view(row, now_ms)) for row in rows),
            query,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        decision_id = "dec-" + uuid.uuid4().hex[:16]
        selected = decision.selected
        logger.info(
            "route %s capability=%s strategy=%s selected=%s score=%s alternates=%d "
            "rejected=%d elapsed_ms=%.2f",
            decision_id,
            capability,
            body.strategy,
            f"{selected['node_id']}/{selected['model']}" if selected else "-",
            f"{selected['score']:.4f}" if selected else "-",
            len(decision.alternates),
            len(decision.rejected),
            elapsed_ms,
        )
        request.app.state.route_metrics.observe(
            strategy=body.strategy,
            selected=selected is not None,
            rejected=decision.rejected,
            duration_s=elapsed_ms / 1000,
        )

        payload: dict[str, Any] = {
            "registry_version": REGISTRY_VERSION,
            "server_time": _iso(now_ms),
            "decision_id": decision_id,
            "decided_at": _iso(now_ms),
            "ttl_ms": cfg.route_ttl_ms,
            "strategy": body.strategy,
            "selected": selected,
            "alternates": decision.alternates,
            "rejected": decision.rejected,
        }
        if decision.reason:
            payload["reason"] = decision.reason
        return payload

    @app.get("/zhentu/v1/metrics", dependencies=[Depends(require_auth)])
    async def metrics_endpoint(request: Request):
        """Prometheus 文本指标；与其它 /v1 端点一样要求 Bearer 鉴权。

        除 `/route` 决策指标外，补充两个节点存量 gauge（总数 / 在线数），
        让灵脉无需额外适配层就能画「在线节点」并告警「全员失联」。
        只暴露计数，不暴露 node_id / model / endpoint（避免高基数与拓扑泄露）。
        """
        total, online = request.app.state.store.counts(request.app.state.clock())
        gauges = (
            "# HELP zhentu_nodes_total 注册表中的节点总数（含离线）。\n"
            "# TYPE zhentu_nodes_total gauge\n"
            f"zhentu_nodes_total {total}\n"
            "# HELP zhentu_nodes_online 在线节点数（租约有效）。\n"
            "# TYPE zhentu_nodes_online gauge\n"
            f"zhentu_nodes_online {online}\n"
        )
        return PlainTextResponse(
            request.app.state.route_metrics.render() + gauges,
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    async def require_node_identity(
        request: Request,
        authorization: str | None = Header(default=None),
        x_node_id: str | None = Header(default=None, alias="X-Node-Id"),
    ) -> str:
        cfg_ = request.app.state.settings
        scheme, _, value = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not value:
            raise RegistryError(401, "unauthorized", "缺少 bearer token")
        if cfg_.node_tokens:
            matched = [
                node_id
                for node_id, token in cfg_.node_tokens.items()
                if secrets.compare_digest(value, token)
            ]
            if not matched:
                raise RegistryError(401, "unauthorized", "bearer token 无匹配节点")
            if len(matched) > 1:
                raise RegistryError(
                    401, "unauthorized", "bearer token 匹配多个节点，配置不唯一"
                )
            identity = matched[0]
            if x_node_id and x_node_id != identity:
                raise RegistryError(403, "identity_mismatch", "X-Node-Id 与 token 不匹配")
            return identity
        if cfg_.token and not secrets.compare_digest(value, cfg_.token.get()):
            raise RegistryError(401, "unauthorized", "缺少或无效的 bearer token")
        if not x_node_id:
            raise RegistryError(
                403, "identity_required", "共享 token 模式必须携带 X-Node-Id"
            )
        return x_node_id

    # ---------------------------------------------------------------- 任务

    def _task_urls(task_id: str) -> dict[str, str]:
        return {
            "events_url": f"/zhentu/v1/requests/{task_id}/events",
            "result_url": f"/zhentu/v1/requests/{task_id}",
        }

    def _task_summary(task: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": task["task_id"],
            "status": task["status"],
            "created_at": _iso(int(task["created_at_ms"])),
            "deadline_at": _iso(int(task["deadline_at_ms"])),
            **_task_urls(task["task_id"]),
        }

    def _canceled_result(task_id: str) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "task_id": task_id,
            "status": "canceled",
            "terminal_reason": "user_canceled",
            "mode": "cluster",
            "answer": None,
            "sources": [],
            "partials": [],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "steps": 0},
            "error": None,
        }

    def _orchestration_error(code: str, message: str) -> dict[str, Any]:
        return {
            "error": {
                "message": message,
                "type": "orchestration_error",
                "param": None,
                "code": code,
            }
        }

    @app.post("/zhentu/v1/requests")
    async def create_task(
        request: Request,
        body: TaskCreateRequest,
        identity: str = Depends(require_node_identity),
    ):
        if body.origin_node_id != identity:
            raise RegistryError(
                403,
                "origin_mismatch",
                "origin_node_id 与鉴权身份不一致",
                type_="orchestration_error",
            )
        if body.privacy == "local_only":
            raise RegistryError(
                403,
                "local_only_rejected",
                "local_only 任务不允许提交到阵图",
                type_="orchestration_error",
            )

        existing = ledger.get_by_request(identity, body.request_id)
        if existing is not None:
            return JSONResponse(
                status_code=200,
                content={**_task_summary(existing), "duplicate": True},
            )
        if ledger.count_active_tasks() >= cfg.max_active_tasks:
            return JSONResponse(
                status_code=429,
                content=_orchestration_error(
                    "task_capacity_exceeded", "任务数已达上限，请稍后重试"
                ),
                headers={"Retry-After": "2"},
            )

        now_ms = now()
        task, created = ledger.create_task(
            request_id=body.request_id,
            origin_node_id=identity,
            session_id=body.session_id,
            mode=body.mode,
            requested_mode=body.requested_mode,
            privacy=body.privacy,
            request=body.model_dump(),
            deadline_at_ms=now_ms + body.deadline_s * 1000,
            now_ms=now_ms,
        )
        if not created:
            return JSONResponse(
                status_code=200,
                content={**_task_summary(task), "duplicate": True},
            )
        ledger.append_event(
            task["task_id"],
            "task/created",
            {"mode": body.mode, "request_id": body.request_id},
            now_ms=now_ms,
            max_events=cfg.task_event_max_per_task,
            retention_ms=cfg.task_event_retention_days * 86_400_000,
        )
        background = asyncio.create_task(runner.run(task["task_id"]))
        app.state.background_tasks.add(background)
        background.add_done_callback(app.state.background_tasks.discard)
        return JSONResponse(status_code=202, content=_task_summary(task))

    @app.get("/zhentu/v1/requests/{task_id}/events")
    async def task_events(
        task_id: str,
        request: Request,
        identity: str = Depends(require_node_identity),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ):
        task = ledger.get_task(task_id)
        if task is None:
            raise RegistryError(
                404, "task_unknown", f"任务 {task_id} 不存在", type_="orchestration_error"
            )
        if task["origin_node_id"] != identity:
            raise RegistryError(
                403,
                "task_forbidden",
                "只能读取本节点发起的任务",
                type_="orchestration_error",
            )
        after_seq = 0
        if last_event_id:
            event = ledger.get_event(task_id, last_event_id)
            if event is None:
                return JSONResponse(
                    status_code=410,
                    content=_orchestration_error(
                        "replay_unavailable", "Last-Event-ID 已超出保留窗口"
                    ),
                )
            after_seq = int(event["seq"])

        async def generate():
            seq = after_seq
            while True:
                events = ledger.list_events(task_id, seq)
                for event in events:
                    seq = int(event["seq"])
                    payload = {
                        "event_id": event["event_id"],
                        "task_id": task_id,
                        "seq": seq,
                        "type": event["type"],
                        "at": _iso(int(event["at_ms"])),
                        "data": event["data"],
                    }
                    yield (
                        f"id: {seq}\n"
                        f"event: {event['type']}\n"
                        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    )
                current = ledger.get_task(task_id)
                if current and current["status"] in (
                    "completed",
                    "partial",
                    "failed",
                    "canceled",
                ):
                    if not ledger.list_events(task_id, seq):
                        break
                await asyncio.sleep(0.05)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/zhentu/v1/requests/{task_id}")
    async def get_task_result(
        task_id: str,
        request: Request,
        identity: str = Depends(require_node_identity),
    ):
        task = ledger.get_task(task_id)
        if task is None:
            raise RegistryError(
                404, "task_unknown", f"任务 {task_id} 不存在", type_="orchestration_error"
            )
        if task["origin_node_id"] != identity:
            raise RegistryError(
                403,
                "task_forbidden",
                "只能读取本节点发起的任务",
                type_="orchestration_error",
            )
        if task["status"] in ("completed", "partial", "failed", "canceled") and task[
            "result_json"
        ]:
            return JSONResponse(status_code=200, content=task["result_json"])
        return JSONResponse(
            status_code=200,
            content={
                **_task_summary(task),
                "error": task["error_json"],
            },
        )

    @app.post("/zhentu/v1/requests/{task_id}/cancel")
    async def cancel_task(
        task_id: str,
        request: Request,
        identity: str = Depends(require_node_identity),
    ):
        task = ledger.get_task(task_id)
        if task is None:
            raise RegistryError(
                404, "task_unknown", f"任务 {task_id} 不存在", type_="orchestration_error"
            )
        if task["origin_node_id"] != identity:
            raise RegistryError(
                403,
                "task_forbidden",
                "只能取消本节点发起的任务",
                type_="orchestration_error",
            )
        if task["status"] in ("completed", "partial", "failed", "canceled"):
            return JSONResponse(status_code=200, content=_task_summary(task))

        now_ms = now()
        if task["status"] == "queued":
            ledger.update_task(
                task_id,
                now_ms=now_ms,
                status="canceled",
                terminal_reason="user_canceled",
                result_json=_canceled_result(task_id),
                finished_at_ms=now_ms,
            )
            ledger.append_event(
                task_id,
                "task/canceled",
                {"reason": "user_canceled"},
                now_ms=now_ms,
                max_events=cfg.task_event_max_per_task,
                retention_ms=cfg.task_event_retention_days * 86_400_000,
            )
            refreshed = ledger.get_task(task_id) or task
            return JSONResponse(status_code=200, content=_task_summary(refreshed))

        ledger.request_cancel(task_id, now_ms=now_ms)
        refreshed = ledger.get_task(task_id) or task
        return JSONResponse(status_code=202, content=_task_summary(refreshed))

    return app


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    try:
        settings = load_settings()
        settings.validate()
    except ConfigError as exc:
        print(f"[ERR] 配置错误，拒绝启动：{exc}")
        raise SystemExit(1)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
    )
