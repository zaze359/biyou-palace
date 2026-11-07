"""准圣节点智能体配置。

优先级：环境变量 > YAML 配置文件 > 默认值。
配置文件路径可用 QUASHENG_CONFIG 指定，默认当前目录下的 config.yaml。
"""
from __future__ import annotations

import ipaddress
import os
import re
import urllib.parse
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

# 契约常量（与 docs/node-agent-contract.md 保持一致）
CONTRACT_VERSION = "1.0"
NODE_ROLE = "quasheng"
NODE_RUNTIME = "ollama"
# 契约 3.2：/v1/models 每项的 owned_by 必须是节点身份
MODEL_OWNER = "biyou-node"

# 上游 provider id：同时作为远程模型 id 前缀
_PROVIDER_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_VISIBILITIES = ("local", "cluster", "both")


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


class Secret:
    """密钥内存包装：任何 repr/str 都只返回 <redacted>。

    上游密钥只允许来自环境变量；进入内存后不再以明文参与日志、错误体、
    health、模型列表或响应头。
    """

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
class UpstreamModel:
    """上游静态模型清单项。"""

    id: str
    capabilities: list[str] = field(default_factory=lambda: ["chat"])
    context_len: int | None = None


@dataclass
class UpstreamSettings:
    """单个 OpenAI 兼容上游（P0：静态清单 + 直通，不做动态发现/回退）。"""

    id: str
    base_url: str
    api_key: Secret | None = None
    api_key_env: str | None = None
    connect_timeout_s: float = 10.0
    timeout_s: float = 120.0
    max_concurrency: int = 4
    visibility: str = "local"
    data_leaves_lan: bool = True
    # 设计稿默认 false：显式 enabled: true 才参与探活、列表与路由
    enabled: bool = False
    models: list[UpstreamModel] = field(default_factory=list)

    def model_for(self, model_id: str) -> UpstreamModel | None:
        for item in self.models:
            if item.id == model_id:
                return item
        return None

    def cluster_visible(self) -> bool:
        return self.visibility in ("cluster", "both")


def _validate_base_url(provider: str, raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ConfigError(f"上游 {provider} 缺少 base_url")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"上游 {provider} 的 base_url 必须是 http(s)://host[:port]/v1 形式")
    if parsed.username or parsed.password:
        raise ConfigError(f"上游 {provider} 的 base_url 不允许包含用户名/密码")
    if parsed.query or parsed.fragment:
        raise ConfigError(f"上游 {provider} 的 base_url 不允许带查询串或片段")
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        raise ConfigError(
            f"上游 {provider} 的 base_url 必须以上游 OpenAI 兼容根 /v1 结尾（例如 "
            "https://relay.example.com/v1），不要包含 /chat/completions"
        )
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _parse_models(provider: str, raw: Any) -> list[UpstreamModel]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(
            f"上游 {provider} 必须配置静态 models 清单（P0 不支持动态发现）"
        )
    out: list[UpstreamModel] = []
    for item in raw:
        if isinstance(item, str):
            model_id, caps, ctx = item.strip(), ["chat"], None
        elif isinstance(item, dict):
            model_id = str(item.get("id") or "").strip()
            caps = list(item.get("capabilities") or ["chat"])
            ctx = item.get("context_len")
        else:
            raise ConfigError(f"上游 {provider} 的 models 项必须是字符串或映射")
        if not model_id:
            raise ConfigError(f"上游 {provider} 的 models 项缺少 id")
        out.append(UpstreamModel(id=model_id, capabilities=[str(c) for c in caps], context_len=ctx))
    return out


def _parse_upstreams(raw: Any) -> list[UpstreamSettings]:
    """解析 upstreams 段。密钥只从环境变量读取，缺失时直接拒绝启动。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("upstreams 必须是列表")

    parsed: list[UpstreamSettings] = []
    seen_ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError("upstreams 的每一项必须是映射")
        provider = str(item.get("id") or "").strip()
        if not _PROVIDER_ID_RE.fullmatch(provider):
            raise ConfigError(
                f"上游 id {provider!r} 非法：必须匹配 [a-z0-9][a-z0-9_-]{{0,31}}"
            )
        if provider in seen_ids:
            raise ConfigError(f"上游 id 重复：{provider}")
        seen_ids.add(provider)

        if "api_key" in item:
            raise ConfigError(
                f"上游 {provider} 不允许在配置文件中写 api_key：密钥只能通过 "
                "api_key_env 指向的环境变量提供"
            )

        base_url = _validate_base_url(provider, item.get("base_url"))
        visibility = str(item.get("visibility") or "local").strip().lower()
        if visibility not in _VISIBILITIES:
            raise ConfigError(
                f"上游 {provider} 的 visibility={visibility!r} 非法，只能是 "
                f"{' / '.join(_VISIBILITIES)}"
            )
        enabled = bool(item.get("enabled", False))

        api_key_env = str(item.get("api_key_env") or "").strip() or None
        api_key: Secret | None = None
        if enabled:
            if not api_key_env:
                raise ConfigError(
                    f"上游 {provider} 已启用但缺少 api_key_env：密钥只能从环境变量读取，"
                    "请配置 api_key_env 并导出对应变量"
                )
            value = os.environ.get(api_key_env)
            if not value:
                raise ConfigError(
                    f"上游 {provider} 的密钥环境变量 {api_key_env} 未设置或为空。"
                    "请先导出该变量再启动节点（密钥不会从配置文件读取）"
                )
            api_key = Secret(value)

        try:
            connect_timeout = float(item.get("connect_timeout_s", 10))
            timeout = float(item.get("timeout_s", 120))
            max_concurrency = int(item.get("max_concurrency", 4))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"上游 {provider} 的超时/并发字段必须是数字") from exc
        if connect_timeout <= 0 or timeout <= 0:
            raise ConfigError(f"上游 {provider} 的 connect_timeout_s / timeout_s 必须大于 0")
        if max_concurrency < 1:
            raise ConfigError(f"上游 {provider} 的 max_concurrency 必须 ≥ 1")

        parsed.append(
            UpstreamSettings(
                id=provider,
                base_url=base_url,
                api_key=api_key,
                api_key_env=api_key_env,
                connect_timeout_s=connect_timeout,
                timeout_s=timeout,
                max_concurrency=max_concurrency,
                visibility=visibility,
                data_leaves_lan=bool(item.get("data_leaves_lan", True)),
                enabled=enabled,
                models=_parse_models(provider, item.get("models")),
            )
        )

    # 远程模型全 id 唯一性：<provider>/<model>
    seen_models: dict[str, str] = {}
    for upstream in parsed:
        for model in upstream.models:
            full_id = f"{upstream.id}/{model.id}"
            if full_id in seen_models:
                raise ConfigError(
                    f"远程模型 id 冲突：{full_id}（上游 {seen_models[full_id]} 与 {upstream.id}）"
                )
            seen_models[full_id] = upstream.id
    return parsed


def check_local_model_conflicts(settings: Settings, local_model_ids: list[str]) -> None:
    """启动期校验：本地模型首段不得与任何已启用上游 id 相同。

    路由采用精确匹配 + provider 前缀；本地模型一旦占用 provider 前缀就会
    产生归属歧义，因此直接拒绝启动并列出冲突项。
    """
    providers = {u.id for u in settings.upstreams if u.enabled}
    conflicts = []
    for model_id in sorted(set(local_model_ids)):
        head = str(model_id).partition("/")[0]
        if head in providers:
            conflicts.append(model_id)
    if conflicts:
        raise ConfigError(
            "本地模型与上游 provider 前缀冲突："
            + "、".join(conflicts)
            + "；远程模型 id 形如 <provider>/<model>，请重命名本地模型或上游 id"
        )


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
    # 外部 OpenAI 兼容上游（默认空：行为与未接入时完全一致）
    upstreams: list[UpstreamSettings] = field(default_factory=list)

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
    settings = Settings(
        **{k: v for k, v in raw.items() if k in known and k != "upstreams"}
    )
    settings.capabilities_map = _normalize_caps(raw.get("capabilities_map"))
    settings.upstreams = _parse_upstreams(raw.get("upstreams"))

    for env_key, (attr, cast) in _ENV_OVERRIDES.items():
        val = os.environ.get(env_key)
        if val:
            setattr(settings, attr, cast(val))

    # 未显式声明 auth_enabled 时：配了 token 就自动开启
    if "auth_enabled" not in raw and settings.shared_token:
        settings.auth_enabled = True

    validate_settings(settings)
    return settings
