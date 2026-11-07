"""LoadingTracker 单测：按需加载窗口、已加载同步、失败清理与 TTL。"""
from __future__ import annotations

from nodes.quasheng.loading import LoadingTracker, model_key


def test_model_key_normalizes_implicit_latest():
    assert model_key("nomic-embed-text") == "nomic-embed-text"
    assert model_key("nomic-embed-text:latest") == "nomic-embed-text"
    assert model_key("qwen2.5:7b-instruct-q4_K_M") == "qwen2.5"
    assert model_key("") == ""


def test_note_then_mark_loaded_clears_loading():
    tracker = LoadingTracker()
    tracker.note_request("cold-model:latest")
    assert tracker.is_loading()
    tracker.mark_loaded("cold-model")
    assert not tracker.is_loading()


def test_note_request_is_noop_for_already_loaded_model():
    tracker = LoadingTracker()
    tracker.set_loaded(["warm-model:latest"])
    tracker.note_request("warm-model")
    assert not tracker.is_loading()


def test_mark_failed_clears_pending():
    tracker = LoadingTracker()
    tracker.note_request("cold-model")
    tracker.mark_failed("cold-model")
    assert not tracker.is_loading()


def test_sync_with_ps_clears_loaded_pending():
    tracker = LoadingTracker()
    tracker.note_request("cold-model")
    tracker.sync(["cold-model:latest"])
    assert not tracker.is_loading()


def test_stale_pending_expires_by_ttl():
    clock = {"now": 0.0}
    tracker = LoadingTracker(ttl=10, clock=lambda: clock["now"])
    tracker.note_request("cold-model")
    assert tracker.is_loading()

    clock["now"] = 12.0
    assert not tracker.is_loading()
