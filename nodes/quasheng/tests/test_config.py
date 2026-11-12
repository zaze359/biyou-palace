"""配置与默认安全单测：回环判定、对外监听强制 token、示例配置一致性。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nodes.quasheng.config import (
    ConfigError,
    Secret,
    Settings,
    UpstreamModel,
    UpstreamSettings,
    check_local_model_conflicts,
    is_loopback,
    load_settings,
)

_EXAMPLE = Path(__file__).parents[1] / "config.example.yaml"


def test_defaults_bind_loopback_and_concurrency():
    settings = Settings()
    assert settings.host == "127.0.0.1"
    assert settings.max_concurrency == 4
    assert settings.shutdown_timeout == 10.0


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]"])
def test_loopback_hosts(host):
    assert is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "", None, "example.com"])
def test_non_loopback_hosts(host):
    assert not is_loopback(host)


def _write_config(tmp_path: Path, text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_non_loopback_without_token_is_rejected(tmp_path, monkeypatch):
    path = _write_config(tmp_path, 'host: "0.0.0.0"\n')
    monkeypatch.setenv("QUASHENG_CONFIG", path)
    for key in ("QUASHENG_HOST", "QUASHENG_SHARED_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    assert "shared_token" in message
    assert "127.0.0.1" in message


def test_non_loopback_with_token_is_allowed(tmp_path, monkeypatch):
    path = _write_config(tmp_path, 'host: "0.0.0.0"\nshared_token: "s3cret"\n')
    monkeypatch.setenv("QUASHENG_CONFIG", path)
    monkeypatch.delenv("QUASHENG_HOST", raising=False)
    monkeypatch.delenv("QUASHENG_SHARED_TOKEN", raising=False)

    settings = load_settings()
    assert settings.host == "0.0.0.0"
    assert settings.auth_enabled is True


def test_env_host_override_applies(tmp_path, monkeypatch):
    path = _write_config(tmp_path, "")
    monkeypatch.setenv("QUASHENG_CONFIG", path)
    monkeypatch.setenv("QUASHENG_HOST", "0.0.0.0")
    monkeypatch.setenv("QUASHENG_SHARED_TOKEN", "env-token")

    settings = load_settings()
    assert settings.host == "0.0.0.0"
    assert settings.shared_token == "env-token"
    assert settings.auth_enabled is True


def test_example_config_uses_loopback_and_real_model_tags():
    raw = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    assert raw["host"] == "127.0.0.1"
    assert raw["max_concurrency"] >= 4
    assert "nomic-embed-text:latest" in raw["capabilities_map"]
    assert raw["capabilities_map"]["nomic-embed-text:latest"]["capabilities"] == ["embedding"]
    # 预热白名单必须来自真实 tag（至少带显式 tag，避免精确匹配落空）
    for model in raw["preload_models"]:
        assert ":" in model


# ---------------------------------------------------------------- 上游配置


_UPSTREAM_YAML = """
upstreams:
  - id: relay-a
    base_url: https://relay.example.com/v1
    enabled: true
    api_key_env: TEST_RELAY_KEY
    visibility: local
    data_leaves_lan: true
    models:
      - gpt-4o-mini
"""


def test_upstream_key_missing_fails_with_actionable_message(tmp_path, monkeypatch):
    path = _write_config(tmp_path, _UPSTREAM_YAML)
    monkeypatch.setenv("QUASHENG_CONFIG", path)
    monkeypatch.delenv("TEST_RELAY_KEY", raising=False)

    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    assert "TEST_RELAY_KEY" in message
    assert "环境变量" in message
    assert "sk-" not in message


def test_upstream_key_from_env_is_wrapped_as_secret(tmp_path, monkeypatch):
    path = _write_config(tmp_path, _UPSTREAM_YAML)
    monkeypatch.setenv("QUASHENG_CONFIG", path)
    monkeypatch.setenv("TEST_RELAY_KEY", "sk-live-value")

    settings = load_settings()
    assert len(settings.upstreams) == 1
    upstream = settings.upstreams[0]
    assert upstream.id == "relay-a"
    assert upstream.model_for("gpt-4o-mini") is not None
    assert isinstance(upstream.api_key, Secret)
    assert upstream.api_key.get() == "sk-live-value"
    assert str(upstream.api_key) == "<redacted>"
    assert "sk-live-value" not in repr(upstream.api_key)


def test_upstream_disabled_does_not_require_env(tmp_path, monkeypatch):
    path = _write_config(tmp_path, _UPSTREAM_YAML.replace("enabled: true", "enabled: false"))
    monkeypatch.setenv("QUASHENG_CONFIG", path)
    monkeypatch.delenv("TEST_RELAY_KEY", raising=False)

    settings = load_settings()
    assert settings.upstreams[0].enabled is False
    assert settings.upstreams[0].api_key is None


def test_upstream_inline_api_key_is_rejected(tmp_path, monkeypatch):
    path = _write_config(
        tmp_path,
        _UPSTREAM_YAML.replace("    api_key_env: TEST_RELAY_KEY", "    api_key: sk-inline"),
    )
    monkeypatch.setenv("QUASHENG_CONFIG", path)
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "api_key_env" in str(excinfo.value)


@pytest.mark.parametrize(
    "text",
    [
        _UPSTREAM_YAML.replace("https://relay.example.com/v1", "relay.example.com/v1"),
        _UPSTREAM_YAML.replace(
            "https://relay.example.com/v1", "https://user:pass@relay.example.com/v1"
        ),
        _UPSTREAM_YAML.replace(
            "https://relay.example.com/v1", "https://relay.example.com/v1/chat/completions"
        ),
        _UPSTREAM_YAML.replace("visibility: local", "visibility: public"),
        _UPSTREAM_YAML.replace("    models:\n      - gpt-4o-mini\n", ""),
    ],
)
def test_invalid_upstream_fields_are_rejected(tmp_path, monkeypatch, text):
    path = _write_config(tmp_path, text)
    monkeypatch.setenv("QUASHENG_CONFIG", path)
    monkeypatch.setenv("TEST_RELAY_KEY", "sk-live-value")
    with pytest.raises(ConfigError):
        load_settings()


def test_duplicate_provider_id_is_rejected(tmp_path, monkeypatch):
    duplicate = _UPSTREAM_YAML + _UPSTREAM_YAML.split("upstreams:\n", 1)[1]
    path = _write_config(tmp_path, duplicate)
    monkeypatch.setenv("QUASHENG_CONFIG", path)
    monkeypatch.setenv("TEST_RELAY_KEY", "sk-live-value")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "上游 id 重复" in str(excinfo.value)


def test_duplicate_remote_model_id_is_rejected(tmp_path, monkeypatch):
    duplicate = _UPSTREAM_YAML + "      - gpt-4o-mini\n"
    path = _write_config(tmp_path, duplicate)
    monkeypatch.setenv("QUASHENG_CONFIG", path)
    monkeypatch.setenv("TEST_RELAY_KEY", "sk-live-value")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "远程模型 id 冲突" in str(excinfo.value)


def test_local_model_prefix_conflict_is_rejected():
    upstream = UpstreamSettings(
        id="relay-a",
        base_url="https://relay.example.com/v1",
        enabled=True,
        api_key=Secret("sk"),
        models=[UpstreamModel(id="gpt-4o-mini")],
    )
    settings = Settings(upstreams=[upstream])

    check_local_model_conflicts(settings, ["llama3:8b"])
    with pytest.raises(ConfigError) as excinfo:
        check_local_model_conflicts(settings, ["relay-a/local-name", "llama3:8b"])
    assert "relay-a/local-name" in str(excinfo.value)


_DISCOVER_YAML = """
upstreams:
  - id: relay-a
    base_url: https://relay.example.com/v1
    enabled: true
    api_key_env: TEST_RELAY_KEY
    discover: true
"""


def test_discover_allows_empty_whitelist_and_default_interval(tmp_path, monkeypatch):
    path = _write_config(tmp_path, _DISCOVER_YAML)
    monkeypatch.setenv("QUASHENG_CONFIG", path)
    monkeypatch.setenv("TEST_RELAY_KEY", "sk-live-value")

    settings = load_settings()
    upstream = settings.upstreams[0]
    assert upstream.discover is True
    assert upstream.discover_interval_s == 300.0
    assert upstream.models == []


def test_discover_interval_below_floor_is_rejected(tmp_path, monkeypatch):
    path = _write_config(tmp_path, _DISCOVER_YAML + "    discover_interval_s: 10\n")
    monkeypatch.setenv("QUASHENG_CONFIG", path)
    monkeypatch.setenv("TEST_RELAY_KEY", "sk-live-value")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "下限" in str(excinfo.value)


_FALLBACK_YAML = """
fallback_mode: auto
fallback_chain:
  - relay-a/gpt-4o-mini
  - local/qwen2.5:0.5b
upstreams:
  - id: relay-a
    base_url: https://relay.example.com/v1
    enabled: true
    api_key_env: TEST_RELAY_KEY
    models:
      - id: gpt-4o-mini
"""


def test_fallback_chain_is_parsed(tmp_path, monkeypatch):
    path = _write_config(tmp_path, _FALLBACK_YAML)
    monkeypatch.setenv("QUASHENG_CONFIG", path)
    monkeypatch.setenv("TEST_RELAY_KEY", "sk-live-value")

    settings = load_settings()
    assert settings.fallback_mode == "auto"
    assert settings.fallback_chain == ["relay-a/gpt-4o-mini", "local/qwen2.5:0.5b"]


def test_fallback_invalid_mode_is_rejected(tmp_path, monkeypatch):
    path = _write_config(tmp_path, "fallback_mode: sometimes\n")
    monkeypatch.setenv("QUASHENG_CONFIG", path)

    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "fallback_mode" in str(excinfo.value)


def test_fallback_static_remote_model_is_deferred_to_startup(tmp_path, monkeypatch):
    """远程候选是否存在属可用性问题，解析阶段只记结构，不阻断启动。"""
    path = _write_config(
        tmp_path,
        _FALLBACK_YAML.replace("relay-a/gpt-4o-mini", "relay-a/not-there"),
    )
    monkeypatch.setenv("QUASHENG_CONFIG", path)
    monkeypatch.setenv("TEST_RELAY_KEY", "sk-live-value")

    settings = load_settings()
    assert settings.fallback_chain == ["relay-a/not-there", "local/qwen2.5:0.5b"]
