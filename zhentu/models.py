"""注册协议的数据模型与校验。

报文与 docs/node-agent-contract.md 的 /v1/health 对齐；能力挂在模型上。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Literal, Sequence
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

CANONICAL_CAPABILITIES = ("chat", "embedding", "vision", "ocr", "rerank")
CAPABILITY_ALIASES = {
    "embeddings": "embedding",
    "embed": "embedding",
    "text-embedding": "embedding",
}
_CAP_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_NODE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_BOOT_ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")

NodeStatus = Literal["ready", "loading", "degraded", "down"]
NodeRole = Literal["quasheng", "sanxian"]


def normalize_capability(value: str) -> str:
    raw = str(value or "").strip().lower()
    raw = CAPABILITY_ALIASES.get(raw, raw)
    if not _CAP_RE.fullmatch(raw):
        raise ValueError(f"能力名非法：{value!r}")
    return raw


def normalize_endpoint(value: str) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("endpoint 必须是 http(s)://host[:port] 形式")
    if parsed.username or parsed.password:
        raise ValueError("endpoint 不允许包含用户名/密码")
    if parsed.query or parsed.fragment:
        raise ValueError("endpoint 不允许带查询串或片段")
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


class NodeMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""
    role: NodeRole
    arch: str | None = None
    runtime: str | None = None
    runtime_version: str | None = None
    contract_version: str = "1.0"

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _NODE_ID_RE.fullmatch(value or ""):
            raise ValueError("node.id 必须匹配 [a-z0-9][a-z0-9._-]{0,63}")
        return value


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    capabilities: list[str] = Field(default_factory=lambda: ["chat"])
    context_len: int | None = None
    loaded: bool = False

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("models[].name 不能为空")
        return name

    @field_validator("capabilities")
    @classmethod
    def _normalize_caps(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for item in values or ["chat"]:
            cap = normalize_capability(item)
            if cap not in out:
                out.append(cap)
        return out or ["chat"]

    @field_validator("context_len")
    @classmethod
    def _check_context(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("models[].context_len 必须大于 0")
        return value


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    node: NodeMeta
    endpoint: str
    status: NodeStatus = "ready"
    models: list[ModelEntry] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)
    heartbeat_interval_s: int = 15
    boot_id: str
    sent_at: str | None = None

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        return normalize_endpoint(value)

    @field_validator("heartbeat_interval_s")
    @classmethod
    def _check_interval(cls, value: int) -> int:
        if not 10 <= value <= 120:
            raise ValueError("heartbeat_interval_s 必须在 10..120 秒")
        return value

    @field_validator("boot_id")
    @classmethod
    def _check_boot_id(cls, value: str) -> str:
        if not _BOOT_ID_RE.fullmatch(value or ""):
            raise ValueError("boot_id 必须是 8..128 位安全字符（字母/数字/._:-）")
        return value

    @field_validator("models")
    @classmethod
    def _check_models(cls, models: list[ModelEntry]) -> list[ModelEntry]:
        if len(models) > 64:
            raise ValueError("models 最多 64 项")
        seen: set[str] = set()
        for model in models:
            if model.name in seen:
                raise ValueError(f"models 存在重复项：{model.name}")
            seen.add(model.name)
        return models


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    boot_id: str
    status: NodeStatus = "ready"
    models_rev: str = ""
    loaded_models: list[str] = Field(default_factory=list)
    resources: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    sent_at: str | None = None

    @field_validator("boot_id")
    @classmethod
    def _check_boot_id(cls, value: str) -> str:
        if not _BOOT_ID_RE.fullmatch(value or ""):
            raise ValueError("boot_id 必须是 8..128 位安全字符（字母/数字/._:-）")
        return value


class DeregisterRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    boot_id: str


class RouteRequest(BaseModel):
    """单跳选路请求；字段全部可选，默认只按 chat 能力做一次决策。"""

    model_config = ConfigDict(extra="ignore")

    capability: str = "chat"
    model: str | None = None
    context_len: int | None = None
    strategy: str = "balanced"
    max_candidates: int = 3
    exclude_nodes: list[str] = Field(default_factory=list)
    exclude_models: list[str] = Field(default_factory=list)
    allowed_models: list[str] | None = None
    allow_loading: bool = False
    allow_shedding: bool = False
    request_id: str | None = None

    @field_validator("model")
    @classmethod
    def _strip_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("context_len")
    @classmethod
    def _check_context_len(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("context_len 必须大于 0")
        return value

    @field_validator("max_candidates")
    @classmethod
    def _check_max_candidates(cls, value: int) -> int:
        return max(1, min(int(value), 10))

    @field_validator("strategy")
    @classmethod
    def _strip_strategy(cls, value: str) -> str:
        return str(value or "").strip().lower()


def models_revision(models: Sequence[ModelEntry]) -> str:
    """模型/能力集合的稳定摘要；loaded 不参与（心跳里单独上报）。

    协议级规范化（与散仙 Kotlin 实现逐字节一致）：
        name|cap1,cap2|context_len
    按 name 排序后用 \\n 连接，再取 SHA-256。
    """
    lines: list[str] = []
    for item in sorted(models, key=lambda entry: entry.name):
        caps = ",".join(sorted(set(item.capabilities)))
        context = "" if item.context_len is None else str(item.context_len)
        lines.append(f"{item.name}|{caps}|{context}")
    raw = "\n".join(lines)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def capability_union(models: Sequence[ModelEntry]) -> list[str]:
    caps: set[str] = set()
    for model in models:
        caps.update(model.capabilities)
    return [cap for cap in CANONICAL_CAPABILITIES if cap in caps] + sorted(
        caps.difference(CANONICAL_CAPABILITIES)
    )


def ttl_for(role: str, interval_s: int, ttl_max_s: int = 600) -> int:
    floor = 90 if role == "quasheng" else 180
    return min(max(3 * interval_s, floor), ttl_max_s)
