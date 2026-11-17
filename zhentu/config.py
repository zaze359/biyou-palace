"""Registry 运行配置：全部来自环境变量，token 不落配置文件。"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

REGISTRY_VERSION = "1.0"
TOKEN_ENV = "ZHENTU_REGISTRY_TOKEN"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 4020
DEFAULT_DB = Path(__file__).parent / "data" / "registry.db"
DEFAULT_MAX_BODY_BYTES = 64 * 1024
DEFAULT_TTL_MAX_S = 600
DEFAULT_ROUTE_TTL_MS = 5000


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
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    ttl_max_s: int = DEFAULT_TTL_MAX_S
    route_ttl_ms: int = DEFAULT_ROUTE_TTL_MS
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


def load_settings() -> RegistrySettings:
    token = os.environ.get(TOKEN_ENV) or ""
    db_path = os.environ.get("ZHENTU_DB") or str(DEFAULT_DB)
    return RegistrySettings(
        host=os.environ.get("ZHENTU_HOST", DEFAULT_HOST),
        port=int(os.environ.get("ZHENTU_PORT", DEFAULT_PORT)),
        db_path=db_path,
        token=Secret(token) if token else None,
        token_env=TOKEN_ENV,
        max_body_bytes=int(os.environ.get("ZHENTU_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)),
        ttl_max_s=int(os.environ.get("ZHENTU_TTL_MAX_S", DEFAULT_TTL_MAX_S)),
        route_ttl_ms=int(os.environ.get("ZHENTU_ROUTE_TTL_MS", DEFAULT_ROUTE_TTL_MS)),
    )
