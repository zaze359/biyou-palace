"""准圣节点智能体配置。

优先级：环境变量 > YAML 配置文件 > 默认值。
配置文件路径可用 QUASHENG_CONFIG 指定，默认当前目录下的 config.yaml。
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

# 契约常量（与 docs/node-agent-contract.md 保持一致）
CONTRACT_VERSION = "1.0"
NODE_ROLE = "quasheng"
NODE_RUNTIME = "ollama"
# 契约 3.2：/v1/models 每项的 owned_by 必须是节点身份
MODEL_OWNER = "biyou-node"


def _normalize_caps(raw: dict | None) -> dict[str, dict]:
    """把 YAML 里的能力声明归一成 {model: {capabilities: [...], context_len: ...}}。

    支持两种写法：
        my-model: [chat, embedding]
        my-model: {capabilities: [chat], context_len: 32768}
    """
    out: dict[str, dict] = {}
    for name, val in (raw or {}).items():
        if isinstance(val, list):
            out[str(name)] = {"capabilities": list(val), "context_len": None}
        elif isinstance(val, dict):
            out[str(name)] = {
                "capabilities": list(val.get("capabilities") or ["chat"]),
                "context_len": val.get("context_len"),
            }
    return out


class ConfigError(ValueError):
    """配置不自洽，节点必须拒绝启动（不是可降级的运行期错误）。"""


def is_loopback(host: str | None) -> bool:
    """判断监听地址是否回环。

    回环等价集：`localhost`、IPv4/IPv6 回环地址。空串/主机名视为非回环
    （uvicorn 传空串会监听全部网卡）。
    """
    if not host:
        return False
    value = host.strip().strip("[]")
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validate_settings(settings: Settings) -> None:
    """启动前的安全校验：对外监听必须带 token。"""
    if not is_loopback(settings.host) and not (settings.shared_token or "").strip():
        raise ConfigError(
            f"监听地址 host={settings.host!r} 不是回环地址，但 shared_token 为空。"
            "对外暴露推理端点必须配置 shared_token（或改为 host: 127.0.0.1）。"
        )


@dataclass
class Settings:
    host: str = "127.0.0.1"
    ollama_base: str = "http://127.0.0.1:11434"
    port: int = 4010
    node_id: str = "quasheng-01"
    node_name: str = "准圣·01"
    shared_token: str | None = None
    auth_enabled: bool = False
    preload_models: list[str] = field(default_factory=list)
    capabilities_map: dict[str, dict] = field(default_factory=dict)
    # 契约四章：准圣建议 ≥ 4
    max_concurrency: int = 4
    mem_warn_pct: float = 85.0
    cpu_warn_pct: float = 90.0
    request_timeout: float = 300.0
    # 优雅停机：等待在途请求 drain 的秒数
    shutdown_timeout: float = 10.0

    def capability_for(self, model: str) -> list[str]:
        """未登记的模型默认只有 chat 能力——能力必须由配置显式声明。"""
        entry = self.capabilities_map.get(model)
        if entry and entry.get("capabilities"):
            return list(entry["capabilities"])
        return ["chat"]

    def context_len_for(self, model: str) -> int | None:
        entry = self.capabilities_map.get(model)
        return entry.get("context_len") if entry else None


_ENV_OVERRIDES = {
    "QUASHENG_HOST": ("host", str),
    "QUASHENG_OLLAMA_BASE": ("ollama_base", str),
    "QUASHENG_PORT": ("port", int),
    "QUASHENG_NODE_ID": ("node_id", str),
    "QUASHENG_NODE_NAME": ("node_name", str),
    "QUASHENG_SHARED_TOKEN": ("shared_token", str),
    "QUASHENG_MAX_CONCURRENCY": ("max_concurrency", int),
    "QUASHENG_SHUTDOWN_TIMEOUT": ("shutdown_timeout", float),
}


def load_settings(path: str | Path | None = None) -> Settings:
    raw: dict = {}
    cfg_path = Path(path) if path else Path(os.environ.get("QUASHENG_CONFIG", "config.yaml"))
    if cfg_path.is_file():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    known = {f.name for f in fields(Settings)}
    settings = Settings(**{k: v for k, v in raw.items() if k in known})
    settings.capabilities_map = _normalize_caps(raw.get("capabilities_map"))

    for env_key, (attr, cast) in _ENV_OVERRIDES.items():
        val = os.environ.get(env_key)
        if val:
            setattr(settings, attr, cast(val))

    # 未显式声明 auth_enabled 时：配了 token 就自动开启
    if "auth_enabled" not in raw and settings.shared_token:
        settings.auth_enabled = True

    validate_settings(settings)
    return settings
