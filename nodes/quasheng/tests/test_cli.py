"""CLI 单测：多轮上下文、/reset、/model 切换与非 2xx 错误体展示。"""
from __future__ import annotations

import json
import sys

import httpx
import pytest

from nodes.quasheng import cli

SSE = (
    'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
    "data: [DONE]\n\n"
)


def test_stream_chat_prints_openai_error_body(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "error": {
                    "message": "model 'nope' not found",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "model_not_found",
                }
            },
        )

    ok, reply = cli._stream_chat(
        "http://upstream",
        None,
        "nope",
        [{"role": "user", "content": "hi"}],
        transport=httpx.MockTransport(handler),
    )
    assert ok is False
    assert reply == ""
    out = capsys.readouterr().out
    assert "HTTP 404" in out
    assert "model_not_found" in out
    assert "model 'nope' not found" in out


def test_format_error_handles_plain_text_and_empty_body():
    assert "HTTP 500" in cli._format_error(500, "internal boom")
    assert "internal boom" in cli._format_error(500, "internal boom")
    assert "（空响应体）" in cli._format_error(500, b"")


def test_stream_chat_sends_full_history_and_prints_content(capsys):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})

    history = [
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "上一轮回答"},
        {"role": "user", "content": "第二轮"},
    ]
    ok, reply = cli._stream_chat(
        "http://upstream", None, "m", history, transport=httpx.MockTransport(handler)
    )
    assert ok is True
    assert reply == "你好"
    assert captured["payload"]["messages"] == history
    assert "你好" in capsys.readouterr().out


def test_repl_keeps_multi_turn_context(monkeypatch):
    calls: list[list[dict]] = []

    def fake_stream(base, token, model, messages, *, transport=None):
        calls.append(list(messages))
        return True, f"reply-{len(calls)}"

    monkeypatch.setattr(cli, "_stream_chat", fake_stream)
    inputs = iter(["first", "second", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))

    cli.run_repl("http://node", None, "m")

    assert calls[0] == [{"role": "user", "content": "first"}]
    assert calls[1] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply-1"},
        {"role": "user", "content": "second"},
    ]


def test_repl_reset_clears_context(monkeypatch):
    calls: list[list[dict]] = []

    def fake_stream(base, token, model, messages, *, transport=None):
        calls.append(list(messages))
        return True, "reply"

    monkeypatch.setattr(cli, "_stream_chat", fake_stream)
    inputs = iter(["one", "/reset", "two", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))

    cli.run_repl("http://node", None, "m")

    assert calls[0] == [{"role": "user", "content": "one"}]
    assert calls[1] == [{"role": "user", "content": "two"}]


def test_repl_switches_model_at_runtime(monkeypatch):
    seen_models: list[str] = []

    def fake_stream(base, token, model, messages, *, transport=None):
        seen_models.append(model)
        return True, "ok"

    monkeypatch.setattr(cli, "_stream_chat", fake_stream)
    inputs = iter(["/model m2", "hi", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))

    cli.run_repl("http://node", None, "m1")

    assert seen_models == ["m2"]


def test_repl_lists_models_when_switch_without_name(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_list_model_ids", lambda base, token, *, transport=None: ["m1", "m2"])
    inputs = iter(["/model", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))

    cli.run_repl("http://node", None, "m1")

    assert "可用模型：m1, m2" in capsys.readouterr().out


def test_once_mode_returns_nonzero_on_error(monkeypatch):
    monkeypatch.setattr(cli, "_pick_model", lambda *a, **k: "m")
    monkeypatch.setattr(cli, "_stream_chat", lambda *a, **k: (False, ""))
    monkeypatch.setattr(sys, "argv", ["cli", "--once", "hi"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 1
