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
from .ollama_client import OllamaClient

# 准圣是 PC：电池 / 温度无意义，按契约恒为 null
_BATTERY = None
_TEMPERATURE = None
_THERMAL = None


def _mb(value: float) -> int:
    return int(value / (1024 * 1024))


def collect_resources(queue_len: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """采集主机资源。

    返回 (契约 resources, 内部指标 {cpu_pct, mem_pct})。
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
) -> str:
    """状态判定（纯函数，便于单测）。优先级：down > loading > degraded > ready。"""
    if not ollama_ok:
        return "down"
    if not preload_loaded:
        return "loading"
    if (mem_pct is not None and mem_pct >= settings.mem_warn_pct) or (
        cpu_pct is not None and cpu_pct >= settings.cpu_warn_pct
    ):
        return "degraded"
    return "ready"


def _model_entry(tag: dict[str, Any], loaded: set[str], settings: Settings) -> dict[str, Any]:
    name = str(tag.get("name") or tag.get("model") or "")
    details = tag.get("details") or {}
    return {
        "name": name,
        "family": details.get("family"),
        "size_label": details.get("parameter_size"),
        "context_len": settings.context_len_for(name),
        "loaded": name in loaded,
        "capabilities": settings.capability_for(name),
    }


async def build_health(
    settings: Settings, client: OllamaClient, gate: ConcurrencyGate
) -> dict[str, Any]:
    ollama_ok = True
    tags: list[dict[str, Any]] = []
    running: list[dict[str, Any]] = []
    try:
        tags = await client.tags()
        running = await client.ps()
    except Exception:  # noqa: BLE001 - 健康检查不得因上游异常而失败
        ollama_ok = False

    loaded = {str(m.get("name") or m.get("model")) for m in running}
    models = [_model_entry(t, loaded, settings) for t in tags] if ollama_ok else []

    resources, metrics = collect_resources(gate.active)
    preload_loaded = (
        all(m in loaded for m in settings.preload_models) if settings.preload_models else True
    )
    status = determine_status(
        ollama_ok=ollama_ok,
        preload_loaded=preload_loaded,
        mem_pct=metrics.get("mem_pct"),
        cpu_pct=metrics.get("cpu_pct"),
        settings=settings,
    )

    known_context = [m["context_len"] for m in models if m.get("context_len")]
    return {
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
