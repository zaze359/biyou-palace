"""配置与默认安全单测：回环判定、对外监听强制 token、示例配置一致性。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nodes.quasheng.config import ConfigError, Settings, is_loopback, load_settings

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
