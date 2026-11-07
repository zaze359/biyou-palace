"""核心纯逻辑单测（不依赖网络与 Ollama）。

覆盖：状态判定优先级、能力声明回退、配置归一。
"""
from __future__ import annotations

from nodes.quasheng.config import Settings, _normalize_caps
from nodes.quasheng.health import determine_status


def _settings(**kwargs) -> Settings:
    return Settings(**kwargs)


def test_status_down_when_ollama_unreachable():
    status = determine_status(
        ollama_ok=False,
        preload_loaded=False,
        mem_pct=10.0,
        cpu_pct=10.0,
        settings=_settings(),
    )
    assert status == "down"


def test_status_loading_when_preload_not_loaded():
    status = determine_status(
        ollama_ok=True,
        preload_loaded=False,
        mem_pct=10.0,
        cpu_pct=10.0,
        settings=_settings(),
    )
    assert status == "loading"


def test_status_loading_when_model_is_loading_on_demand():
    status = determine_status(
        ollama_ok=True,
        preload_loaded=True,
        mem_pct=10.0,
        cpu_pct=10.0,
        settings=_settings(),
        on_demand_loading=True,
    )
    assert status == "loading"


def test_status_degraded_when_memory_over_threshold():
    status = determine_status(
        ollama_ok=True,
        preload_loaded=True,
        mem_pct=92.0,
        cpu_pct=10.0,
        settings=_settings(mem_warn_pct=85.0),
    )
    assert status == "degraded"


def test_status_ready_when_all_good():
    status = determine_status(
        ollama_ok=True,
        preload_loaded=True,
        mem_pct=40.0,
        cpu_pct=20.0,
        settings=_settings(),
    )
    assert status == "ready"


def test_capability_defaults_to_chat_for_unregistered_model():
    settings = _settings()
    assert settings.capability_for("some-unregistered-model") == ["chat"]


def test_capability_from_map():
    settings = _settings(
        capabilities_map=_normalize_caps({"m1": ["chat", "embedding"], "m2": {"capabilities": ["ocr"]}})
    )
    assert settings.capability_for("m1") == ["chat", "embedding"]
    assert settings.capability_for("m2") == ["ocr"]
    assert settings.context_len_for("m2") is None


def test_normalize_caps_accepts_context_len():
    caps = _normalize_caps({"m": {"capabilities": ["chat"], "context_len": 32768}})
    assert caps["m"]["context_len"] == 32768
