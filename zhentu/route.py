"""阵图单跳选路决策引擎。

纯函数：输入节点视图与查询约束，输出排序后的候选与拒绝原因；
不读写存储、不访问网络，也不返回 endpoint 或任何凭据。

过滤/评分口径见 `.trellis/tasks/11-16-zhentu-orchestration/design.md` 第 3 节。
所有缺失字段都取中性值或保守估算，保证「字段不全」不会变成 500。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# 策略权重之和固定为 1；P3 不开放逐请求自定义权重，只允许选择策略。
STRATEGY_WEIGHTS: dict[str, dict[str, float]] = {
    "balanced": {
        "load": 0.35,
        "freshness": 0.15,
        "loaded": 0.20,
        "thermal": 0.15,
        "role": 0.10,
        "headroom": 0.05,
    },
    "latency": {
        "load": 0.45,
        "freshness": 0.25,
        "loaded": 0.10,
        "thermal": 0.05,
        "role": 0.10,
        "headroom": 0.05,
    },
    "local_first": {
        "load": 0.25,
        "freshness": 0.10,
        "loaded": 0.20,
        "thermal": 0.10,
        "role": 0.30,
        "headroom": 0.05,
    },
    "power_saving": {
        "load": 0.25,
        "freshness": 0.10,
        "loaded": 0.15,
        "thermal": 0.35,
        "role": 0.10,
        "headroom": 0.05,
    },
}

STRATEGY_ROLE_SCORES: dict[str, dict[str, float]] = {
    "balanced": {"quasheng": 0.6, "sanxian": 0.5},
    "latency": {"quasheng": 0.9, "sanxian": 0.6},
    "local_first": {"quasheng": 1.0, "sanxian": 0.3},
    "power_saving": {"quasheng": 1.0, "sanxian": 0.2},
}

STRATEGIES = tuple(STRATEGY_WEIGHTS)
DEFAULT_STRATEGY = "balanced"
DEFAULT_MAX_CANDIDATES = 3
MAX_CANDIDATES = 10
MAX_ALLOWED_MODELS = 200
ASSUMED_CONCURRENCY = 8.0
_ROLE_NEUTRAL = 0.5
_THERMAL_NEUTRAL = 0.5
_NEWAPI_PREFIX = "newapi/"


def bare_model(value: str) -> str:
    """去掉可选 `newapi/` 前缀，让排除/允许列表对两种写法都生效。"""
    text = str(value or "").strip()
    if text.lower().startswith(_NEWAPI_PREFIX):
        return text[len(_NEWAPI_PREFIX) :]
    return text


@dataclass(frozen=True)
class RouteQuery:
    capability: str = "chat"
    model: str | None = None
    context_len: int | None = None
    strategy: str = DEFAULT_STRATEGY
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    exclude_nodes: frozenset[str] = frozenset()
    exclude_models: frozenset[str] = frozenset()
    allowed_models: frozenset[str] | None = None
    allow_loading: bool = False
    allow_shedding: bool = False


@dataclass
class Decision:
    selected: dict[str, Any] | None
    alternates: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None


@dataclass
class _Candidate:
    payload: dict[str, Any]
    score: float
    queue_len: float
    last_seen_ms: int
    node_id: str
    model: str


def decide(
    views: Iterable[tuple[Mapping[str, Any], Mapping[str, Any]]],
    query: RouteQuery,
) -> Decision:
    """对 (row, view) 列表做过滤、评分与确定性排序。"""
    weights = STRATEGY_WEIGHTS[query.strategy]
    role_scores = STRATEGY_ROLE_SCORES[query.strategy]
    pool: list[_Candidate] = []
    rejected: list[dict[str, Any]] = []

    for row, view in views:
        node = view.get("node") or {}
        node_id = str(node.get("id") or row.get("node_id") or "")
        lease = view.get("lease") or {}
        for entry in view.get("models") or []:
            name = str(entry.get("name") or "")
            if not name or query.capability not in (entry.get("capabilities") or []):
                continue
            # 指定单一模型时，其他模型不算「被拒绝」，直接不参与决策。
            if query.model and name != query.model:
                continue
            reason = _reject_reason(view, entry, name, node_id, lease, query)
            if reason:
                rejected.append({"node_id": node_id, "model": name, "reason": reason})
                continue
            pool.append(
                _candidate(row, view, entry, name, node_id, weights, role_scores, query)
            )

    # score 降序 → queue_len 升序 → last_seen 降序 → node_id → model，
    # 保证同一输入在任意阵图实例上得到完全一致的结果。
    pool.sort(
        key=lambda item: (
            -item.score,
            item.queue_len,
            -item.last_seen_ms,
            item.node_id,
            item.model,
        )
    )
    # 夹到 1..10；0 也按下限 1 处理（与 API 层 RouteRequest 夹取口径一致）。
    requested = DEFAULT_MAX_CANDIDATES if query.max_candidates is None else query.max_candidates
    limit = max(1, min(int(requested), MAX_CANDIDATES))
    ordered = [item.payload for item in pool[:limit]]
    rejected.sort(key=lambda item: (item["node_id"], item["model"]))
    return Decision(
        selected=ordered[0] if ordered else None,
        alternates=ordered[1:],
        rejected=rejected,
        reason=None if ordered else "no_route",
    )


def _reject_reason(
    view: Mapping[str, Any],
    entry: Mapping[str, Any],
    name: str,
    node_id: str,
    lease: Mapping[str, Any],
    query: RouteQuery,
) -> str | None:
    """硬过滤；返回脱敏原因码，None 表示通过。顺序与设计稿第 3.2 节一致。"""
    state = lease.get("state")
    if state != "online":
        return "deregistered" if state == "deregistered" else "offline"

    status = str(view.get("status") or "down")
    if status == "down":
        return "status_down"
    if status == "loading" and not query.allow_loading:
        return "status_loading"

    if query.allowed_models is not None and bare_model(name) not in query.allowed_models:
        return "model_not_allowed"

    context_len = _number(entry.get("context_len"))
    if (
        query.context_len
        and context_len is not None
        and context_len < float(query.context_len)
    ):
        return "context_too_small"

    if node_id in query.exclude_nodes:
        return "node_excluded"
    if bare_model(name) in query.exclude_models:
        return "model_excluded"

    resources = view.get("resources") or {}
    if _truthy(resources.get("thermal_shedding")) and not query.allow_shedding:
        return "thermal_shedding"

    if _zero_limit(view.get("limits") or {}):
        return "limit_zero"
    return None


def _candidate(
    row: Mapping[str, Any],
    view: Mapping[str, Any],
    entry: Mapping[str, Any],
    name: str,
    node_id: str,
    weights: Mapping[str, float],
    role_scores: Mapping[str, float],
    query: RouteQuery,
) -> _Candidate:
    resources = view.get("resources") or {}
    limits = view.get("limits") or {}
    lease = view.get("lease") or {}
    reasons: list[str] = []
    estimated = False

    queue_len = _number(resources.get("queue_len"))
    if queue_len is None:
        queue_len = 0.0
    active = _number(resources.get("active"))
    if active is None:
        # 契约 v1.1 节点可能把在途并发放在 queue_len 里：退回按 queue_len 估算，
        # 且不能再与 queue_len 相加，否则在途数会被重复计数。
        active = 0.0
        estimated = True

    effective = _number(limits.get("effective_max_concurrency"))
    if not effective or effective <= 0:
        effective = _number(limits.get("max_concurrency"))
    if not effective or effective <= 0:
        effective = ASSUMED_CONCURRENCY
        estimated = True
    if estimated:
        reasons.append("estimated_load")

    in_flight = active + queue_len
    util = min(1.0, in_flight / effective)
    load = 1.0 - util
    headroom = max(0.0, (effective - in_flight) / effective)

    age_s = _number(lease.get("age_s")) or 0.0
    ttl_s = _number(lease.get("ttl_s")) or 0.0
    freshness = 0.0 if ttl_s <= 0 else _clamp(1.0 - age_s / ttl_s)

    loaded = 1.0 if entry.get("loaded") else 0.4
    thermal = _thermal_score(resources)
    role = role_scores.get(str((view.get("node") or {}).get("role") or ""), _ROLE_NEUTRAL)

    # ready 优先、degraded/loading 降权（设计稿 3.1）；权重表本身不含状态分量。
    status = str(view.get("status") or "ready")
    status_factor = {"ready": 1.0, "degraded": 0.85, "loading": 0.7}.get(status, 1.0)
    score = round(
        (
            weights["load"] * load
            + weights["freshness"] * freshness
            + weights["loaded"] * loaded
            + weights["thermal"] * thermal
            + weights["role"] * role
            + weights["headroom"] * headroom
        )
        * status_factor,
        4,
    )

    if status in ("degraded", "loading"):
        reasons.append(f"status={status}")

    reasons.append("loaded" if entry.get("loaded") else "cold")
    reasons.append(f"queue={_fmt(queue_len)}")
    reasons.append(f"util={util:.2f}")
    reasons.append(f"lease_age={_fmt(age_s)}s")
    if entry.get("context_len") is None and query.context_len:
        reasons.append("context_unknown")
    thermal_state = str(resources.get("thermal_state") or "").lower()
    if thermal_state in ("warn", "severe"):
        reasons.append(f"thermal={thermal_state}")

    node = view.get("node") or {}
    payload = {
        "node_id": node_id,
        "node_name": node.get("name") or "",
        "role": node.get("role"),
        "model": name,
        "capabilities": list(entry.get("capabilities") or []),
        "score": score,
        "reasons": reasons,
    }
    return _Candidate(
        payload=payload,
        score=score,
        queue_len=queue_len,
        last_seen_ms=int(row.get("last_seen_ms") or 0),
        node_id=node_id,
        model=name,
    )


def _thermal_score(resources: Mapping[str, Any]) -> float:
    """热分量：取温度与电量的较低值；warn/severe 再压低上限。"""
    values: list[float] = []
    temperature = _number(resources.get("temperature_c"))
    if temperature is not None:
        if temperature <= 45:
            values.append(1.0)
        elif temperature >= 55:
            values.append(0.0)
        else:
            values.append((55.0 - temperature) / 10.0)
    battery = _number(resources.get("battery_pct"))
    if battery is not None:
        if battery > 30:
            values.append(1.0)
        elif battery >= 10:
            values.append(0.6)
        else:
            values.append(0.2)
    score = min(values) if values else _THERMAL_NEUTRAL
    state = str(resources.get("thermal_state") or "").lower()
    if state == "severe":
        score = min(score, 0.2)
    elif state == "warn":
        score = min(score, 0.5)
    return score


def _zero_limit(limits: Mapping[str, Any]) -> bool:
    for key in ("effective_max_concurrency", "max_concurrency"):
        value = limits.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value == 0:
            return True
    return False


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"
