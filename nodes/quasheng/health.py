"""构建 /v1/health —— 节点**原生**拼装（Ollama 没有这个端点）。

数据来源：/api/tags（模型清单）+ /api/ps（已加载模型）+ psutil（主机资源）。
"""
from __future__ import annotations

import platform
from datetime import datetime
from typing import Any

import psutil

from .config import CONTRACT_VERSION, NODE_ROLE, NODE_RUNTIME, Settings
from .concurrency import ConcurrencyGate
from .loading import LoadingTracker, model_key
from .ollama_client import OllamaClient

# 准圣是 PC：电池 / 温度无意义，按契约恒为 null
_BATTERY = None
_TEMPERATURE = None
_THERMAL = None


def _mb(value: float) -> int:
    return int(value / (1024 * 1024))


def collect_resources(queue_len: int, active: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """采集主机资源。

    返回 (契约 resources, 内部指标 {cpu_pct, mem_pct})。
    `queue_len` 是真实排队数（契约 3.1）；`active` 是节点内部的在途并发，
    作为附加字段供面板/排障使用，不改变原有契约字段。
    任一采集失败降级为 null，绝不抛异常（资源采集失败不得导致 500）。
    """
    try:
        cpu_pct = float(psutil.cpu_percent(interval=None))
    except Exception:
        cpu_pct = None

    mem_used_mb = mem_total_mb = None
    mem_pct = None
    try:
        mem = psutil.virtual_memory()
        mem_used_mb, mem_total_mb = _mb(mem.used), _mb(mem.total)
        mem_pct = float(mem.percent)
    except Exception:
        pass

    try:
        disk_free_mb = _mb(psutil.disk_usage("/").free)
    except Exception:
        disk_free_mb = None

    resources = {
        "cpu_pct": cpu_pct,
        "mem_used_mb": mem_used_mb,
        "mem_total_mb": mem_total_mb,
        "disk_free_mb": disk_free_mb,
        "queue_len": queue_len,
        "active": active,
        "battery_pct": _BATTERY,
        "temperature_c": _TEMPERATURE,
        "thermal_state": _THERMAL,
    }
    return resources, {"cpu_pct": cpu_pct, "mem_pct": mem_pct}


def determine_status(
    *,
    ollama_ok: bool,
    preload_loaded: bool,
    mem_pct: float | None,
    cpu_pct: float | None,
    settings: Settings,
    on_demand_loading: bool = False,
    upstream_statuses: list[str] | None = None,
) -> str:
    """状态判定（纯函数，便于单测）。优先级：down > loading > degraded > ready。

    `loading` 覆盖两类窗口：预热白名单未就绪，以及按需加载中的模型
    （`on_demand_loading`，由 LoadingTracker 提供）。

    上游只降级、不判 down：本地不可用但远程仍可用时节点仍是 degraded，
    否则网关会把还能服务远程模型的节点整体剔除。未配置上游时
    （`upstream_statuses` 为空/None）判定与现状完全一致。
    """
    statuses = upstream_statuses or []
    if not ollama_ok:
        # 本地基线不可用；只要还有可用远程就仍是 degraded
        return "degraded" if any(s == "ready" for s in statuses) else "down"
    if not preload_loaded or on_demand_loading:
        return "loading"
    if any(s != "ready" for s in statuses):
        return "degraded"
    if (mem_pct is not None and mem_pct >= settings.mem_warn_pct) or (
        cpu_pct is not None and cpu_pct >= settings.cpu_warn_pct
    ):
        return "degraded"
    return "ready"


def _model_entry(
    tag: dict[str, Any],
    loaded: set[str],
    settings: Settings,
    *,
    include_source: bool = False,
) -> dict[str, Any]:
    name = str(tag.get("name") or tag.get("model") or "")
    details = tag.get("details") or {}
    entry = {
        "name": name,
        "family": details.get("family"),
        "size_label": details.get("parameter_size"),
        "context_len": settings.context_len_for(name),
        "loaded": name in loaded,
        "capabilities": settings.capability_for(name),
    }
    if include_source:
        # 可选字段：仅在配置了上游时出现，未配置时 health 与现状逐字段一致
        entry.update(
            {
                "provider": None,
                "source": "local",
                "available": True,
                "upstream_status": None,
                "data_leaves_lan": False,
            }
        )
    return entry


async def build_health(
    settings: Settings,
    client: OllamaClient,
    gate: ConcurrencyGate,
    loading: LoadingTracker | None = None,
    warmup_failed: set[str] | None = None,
    upstream_states: list[dict[str, Any]] | None = None,
    upstream_models: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构建 health。

    `upstream_states` / `upstream_models` 为 None 表示未配置上游：此时输出
    与接入前逐字段一致。配置上游后，本地模型补 provider/source 等可选字段，
    远程模型追加到 models[]，并新增顶层 upstreams[]。
    """
    include_source = upstream_states is not None
    ollama_ok = True
    tags: list[dict[str, Any]] = []
    running: list[dict[str, Any]] = []
    try:
        tags = await client.tags()
        running = await client.ps()
    except Exception:  # noqa: BLE001 - 健康检查不得因上游异常而失败
        ollama_ok = False

    loaded = {str(m.get("name") or m.get("model")) for m in running}
    loaded_keys = {model_key(name) for name in loaded}
    models = (
        [_model_entry(t, loaded, settings, include_source=include_source) for t in tags]
        if ollama_ok
        else []
    )
    if upstream_models:
        models.extend(upstream_models)

    if loading is not None:
        loading.sync(loaded)
    on_demand_loading = loading.is_loading() if loading is not None else False

    resources, metrics = collect_resources(gate.waiting, gate.active)

    # 预热判定只统计真实存在于 /api/tags 的模型，并排除已知预热失败的模型：
    # 配置里写了不存在/加载失败的模型不应让节点长期停留在 loading。
    tag_keys = {model_key(str(t.get("name") or t.get("model") or "")) for t in tags}
    failed_keys = {model_key(m) for m in (warmup_failed or set())}
    pending_preload = [
        m
        for m in settings.preload_models
        if model_key(m) in tag_keys
        and model_key(m) not in loaded_keys
        and model_key(m) not in failed_keys
    ]
    preload_loaded = not pending_preload

    status = determine_status(
        ollama_ok=ollama_ok,
        preload_loaded=preload_loaded,
        mem_pct=metrics.get("mem_pct"),
        cpu_pct=metrics.get("cpu_pct"),
        settings=settings,
        on_demand_loading=on_demand_loading,
        upstream_statuses=[str(s.get("status")) for s in (upstream_states or [])],
    )

    known_context = [m["context_len"] for m in models if m.get("context_len")]
    payload = {
        "node": {
            "id": settings.node_id,
            "name": settings.node_name,
            "role": NODE_ROLE,
            "arch": platform.machine(),
            "runtime": NODE_RUNTIME,
            "runtime_version": await client.version() if ollama_ok else None,
            "contract_version": CONTRACT_VERSION,
        },
        "status": status,
        "models": models,
        "resources": resources,
        "limits": {
            "max_concurrency": gate.limit,
            "max_context_len": max(known_context) if known_context else None,
        },
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    if upstream_states is not None:
        # 未配置上游时省略该键：health 与接入前逐字段一致
        payload["upstreams"] = upstream_states
    return payload
