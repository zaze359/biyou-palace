"""Registry 运行配置：全部来自环境变量，token 不落配置文件。"""
from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

REGISTRY_VERSION = "1.0"
TOKEN_ENV = "ZHENTU_REGISTRY_TOKEN"
NODE_TOKENS_ENV = "ZHENTU_NODE_TOKENS"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 4020
DEFAULT_DB = Path(__file__).parent / "data" / "registry.db"
DEFAULT_MAX_BODY_BYTES = 64 * 1024
DEFAULT_TTL_MAX_S = 600
DEFAULT_ROUTE_TTL_MS = 5000
DEFAULT_MAX_ACTIVE_TASKS = 4
DEFAULT_EVENT_RETENTION_DAYS = 30
DEFAULT_EVENT_MAX_PER_TASK = 10_000
DEFAULT_NEWAPI_TIMEOUT_S = 120.0
DEFAULT_NEWAPI_CONNECT_TIMEOUT_S = 5.0
DEFAULT_AGGREGATOR_TIMEOUT_S = 30.0


class ConfigError(ValueError):
    """配置不自洽，Registry 必须拒绝启动。"""


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    value = host.strip().strip("[]")
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


class Secret:
    """token 内存包装：repr/str 永远不暴露明文。"""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:  # pragma: no cover - 防误打印
        return "<redacted>"

    __str__ = __repr__


@dataclass
class RegistrySettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    db_path: str = str(DEFAULT_DB)
    token: Secret | None = None
    token_env: str = TOKEN_ENV
    node_tokens: dict[str, str] = field(default_factory=dict)
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    ttl_max_s: int = DEFAULT_TTL_MAX_S
    route_ttl_ms: int = DEFAULT_ROUTE_TTL_MS
    max_active_tasks: int = DEFAULT_MAX_ACTIVE_TASKS
    task_event_retention_days: int = DEFAULT_EVENT_RETENTION_DAYS
    task_event_max_per_task: int = DEFAULT_EVENT_MAX_PER_TASK
    newapi_base_url: str = ""
    newapi_token: Secret | None = None
    newapi_timeout_s: float = DEFAULT_NEWAPI_TIMEOUT_S
    newapi_connect_timeout_s: float = DEFAULT_NEWAPI_CONNECT_TIMEOUT_S
    planner_model: str = ""
    planner_retries: int = 1
    planner_timeout_s: float = 30.0
    aggregator_mode: str = "concat"
    aggregator_model: str = ""
    aggregator_timeout_s: float = DEFAULT_AGGREGATOR_TIMEOUT_S
    started_ms: int = field(default=0)

    def validate(self) -> None:
        if not (0 < self.port < 65536):
            raise ConfigError(f"ZHENTU_PORT={self.port} 非法：必须在 1..65535")
        if self.max_body_bytes < 1024:
            raise ConfigError("ZHENTU_MAX_BODY_BYTES 不能小于 1024")
        if self.route_ttl_ms <= 0:
            raise ConfigError("ZHENTU_ROUTE_TTL_MS 必须大于 0")
        if not is_loopback(self.host) and not self.token:
            raise ConfigError(
                f"监听地址 host={self.host!r} 不是回环地址，但 {self.token_env} 为空。"
                "对外暴露注册表必须配置 Bearer token（或改为 127.0.0.1）。"
            )
        if self.max_active_tasks < 1:
            raise ConfigError("ZHENTU_MAX_ACTIVE_TASKS 必须大于 0")
        if self.task_event_retention_days < 1:
            raise ConfigError("ZHENTU_TASK_EVENT_RETENTION_DAYS 必须大于 0")
        if self.task_event_max_per_task < 100:
            raise ConfigError("ZHENTU_TASK_EVENT_MAX_PER_TASK 不能小于 100")
        if self.newapi_timeout_s <= 0 or self.newapi_connect_timeout_s <= 0:
            raise ConfigError("NewAPI 超时配置必须大于 0")
        if self.planner_timeout_s <= 0:
            raise ConfigError("ZHENTU_PLANNER_TIMEOUT_S 必须大于 0")
        if self.planner_retries < 0 or self.planner_retries > 3:
            raise ConfigError("ZHENTU_PLANNER_RETRIES 必须在 0..3")
        if self.aggregator_mode not in ("concat", "llm"):
            raise ConfigError("ZHENTU_AGGREGATOR_MODE 只能是 concat 或 llm")
        if self.aggregator_timeout_s <= 0:
            raise ConfigError("ZHENTU_AGGREGATOR_TIMEOUT_S 必须大于 0")
        for node_id, token in self.node_tokens.items():
            if not node_id.strip() or not token.strip():
                raise ConfigError("ZHENTU_NODE_TOKENS 的节点 id 与 token 都不能为空")
        if self.newapi_base_url:
            parsed = urlsplit(self.newapi_base_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ConfigError("ZHENTU_NEWAPI_BASE_URL 必须是 http(s)://host[:port]")
            if parsed.query or parsed.fragment:
                raise ConfigError("ZHENTU_NEWAPI_BASE_URL 不允许带查询串或片段")
            if not self.newapi_token:
                raise ConfigError(
                    "已配置 ZHENTU_NEWAPI_BASE_URL，但 ZHENTU_NEWAPI_API_KEY 为空"
                )


def _load_node_tokens(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{NODE_TOKENS_ENV} 不是合法 JSON：{exc.msg}") from None
    if not isinstance(parsed, dict):
        raise ConfigError(f"{NODE_TOKENS_ENV} 必须是 JSON 对象（node_id → token）")
    return {str(key): str(value) for key, value in parsed.items()}


def load_settings() -> RegistrySettings:
    token = os.environ.get(TOKEN_ENV) or ""
    db_path = os.environ.get("ZHENTU_DB") or str(DEFAULT_DB)
    newapi_key = os.environ.get("ZHENTU_NEWAPI_API_KEY") or ""
    return RegistrySettings(
        host=os.environ.get("ZHENTU_HOST", DEFAULT_HOST),
        port=int(os.environ.get("ZHENTU_PORT", DEFAULT_PORT)),
        db_path=db_path,
        token=Secret(token) if token else None,
        token_env=TOKEN_ENV,
        node_tokens=_load_node_tokens(os.environ.get(NODE_TOKENS_ENV) or ""),
        max_body_bytes=int(os.environ.get("ZHENTU_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)),
        ttl_max_s=int(os.environ.get("ZHENTU_TTL_MAX_S", DEFAULT_TTL_MAX_S)),
        route_ttl_ms=int(os.environ.get("ZHENTU_ROUTE_TTL_MS", DEFAULT_ROUTE_TTL_MS)),
        max_active_tasks=int(
            os.environ.get("ZHENTU_MAX_ACTIVE_TASKS", DEFAULT_MAX_ACTIVE_TASKS)
        ),
        task_event_retention_days=int(
            os.environ.get(
                "ZHENTU_TASK_EVENT_RETENTION_DAYS", DEFAULT_EVENT_RETENTION_DAYS
            )
        ),
        task_event_max_per_task=int(
            os.environ.get("ZHENTU_TASK_EVENT_MAX_PER_TASK", DEFAULT_EVENT_MAX_PER_TASK)
        ),
        newapi_base_url=(os.environ.get("ZHENTU_NEWAPI_BASE_URL") or "").rstrip("/"),
        newapi_token=Secret(newapi_key) if newapi_key else None,
        newapi_timeout_s=float(
            os.environ.get("ZHENTU_NEWAPI_TIMEOUT_S", DEFAULT_NEWAPI_TIMEOUT_S)
        ),
        newapi_connect_timeout_s=float(
            os.environ.get(
                "ZHENTU_NEWAPI_CONNECT_TIMEOUT_S", DEFAULT_NEWAPI_CONNECT_TIMEOUT_S
            )
        ),
        planner_model=os.environ.get("ZHENTU_PLANNER_MODEL") or "",
        planner_retries=int(os.environ.get("ZHENTU_PLANNER_RETRIES") or 1),
        planner_timeout_s=float(os.environ.get("ZHENTU_PLANNER_TIMEOUT_S") or 30.0),
        aggregator_mode=(os.environ.get("ZHENTU_AGGREGATOR_MODE") or "concat").lower(),
        aggregator_model=os.environ.get("ZHENTU_AGGREGATOR_MODEL") or "",
        aggregator_timeout_s=float(
            os.environ.get("ZHENTU_AGGREGATOR_TIMEOUT_S", DEFAULT_AGGREGATOR_TIMEOUT_S)
        ),
    )
