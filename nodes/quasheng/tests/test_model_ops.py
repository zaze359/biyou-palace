"""模型生命周期 CLI 单测：全部用 mock Ollama / mock registry，不依赖真实模型。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from nodes.quasheng import cli, model_ops

GB = 1024**3


def _plenty(monkeypatch, *, disk_free: int = 100 * GB, mem_free: int = 64 * GB) -> None:
    """替身磁盘/内存：避免预检读到跑测试机器的真实余量。"""
    monkeypatch.setattr(
        model_ops.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=disk_free, used=0, free=disk_free),
    )
    monkeypatch.setattr(
        model_ops.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(available=mem_free),
    )


def _ollama(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _registry(size: int | None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if size is None:
            return httpx.Response(404)
        return httpx.Response(200, json={"layers": [{"size": size}]})

    return httpx.MockTransport(handler)


def _ndjson(events: list[dict]) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


# ---------------------------------------------------------------- list


def test_list_marks_loaded_models(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "qwen2.5:0.5b",
                            "size": 660 * 1024 * 1024,
                            "details": {"parameter_size": "0.5B", "quantization_level": "Q4_K_M"},
                        },
                        {
                            "name": "nomic-embed-text:latest",
                            "size": 137 * 1024 * 1024,
                            "details": {"parameter_size": "137M"},
                        },
                    ]
                },
            )
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:0.5b"}]})
        return httpx.Response(404)

    assert cli.cmd_list("http://ollama", transport=_ollama(handler)) == 0

    lines = capsys.readouterr().out.splitlines()
    qwen = next(line for line in lines if "qwen2.5:0.5b" in line)
    embed = next(line for line in lines if "nomic-embed-text:latest" in line)
    assert "已加载" in qwen and "Q4_K_M" in qwen
    assert "未加载" in embed


def test_list_degrades_when_ps_fails(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "m", "size": 1}]})
        return httpx.Response(500, json={"error": "ps boom"})

    assert cli.cmd_list("http://ollama", transport=_ollama(handler)) == 0

    captured = capsys.readouterr()
    assert "已加载状态未知" in captured.err
    assert "未加载" in captured.out


def test_list_empty(capsys):
    transport = _ollama(lambda _r: httpx.Response(200, json={"models": []}))
    assert cli.cmd_list("http://ollama", transport=transport) == 0
    assert "（无已安装模型）" in capsys.readouterr().out


def test_list_upstream_error():
    transport = _ollama(lambda _r: httpx.Response(500, json={"error": "tags boom"}))
    with pytest.raises(model_ops.ModelOpError) as excinfo:
        cli.cmd_list("http://ollama", transport=transport)
    assert "读取已安装模型" in str(excinfo.value)
    assert "tags boom" in str(excinfo.value)


# ---------------------------------------------------------------- pull


def test_pull_success(monkeypatch, capsys):
    _plenty(monkeypatch)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200, text=_ndjson([{"status": "pulling manifest"}, {"status": "success"}])
        )

    code = cli.cmd_pull(
        "qwen2.5:0.5b",
        "http://ollama",
        transport=_ollama(handler),
        registry_transport=_registry(400 * 1024 * 1024),
    )

    assert code == 0
    assert calls == ["/api/pull"]
    out = capsys.readouterr().out
    assert "pulling manifest" in out
    assert "已拉取模型 qwen2.5:0.5b" in out


def test_pull_skips_preflight_when_size_unknown(monkeypatch, capsys):
    _plenty(monkeypatch, disk_free=1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_ndjson([{"status": "success"}]))

    code = cli.cmd_pull(
        "private/model:latest",
        "http://ollama",
        transport=_ollama(handler),
        registry_transport=_registry(None),
    )

    assert code == 0
    assert "跳过磁盘/内存预检" in capsys.readouterr().err


def test_pull_rejects_when_disk_insufficient(monkeypatch):
    _plenty(monkeypatch, disk_free=1 * GB)
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        return httpx.Response(200, text=_ndjson([{"status": "success"}]))

    with pytest.raises(model_ops.ModelOpError) as excinfo:
        cli.cmd_pull(
            "qwen2.5:7b",
            "http://ollama",
            transport=_ollama(handler),
            registry_transport=_registry(10 * GB),
        )

    message = str(excinfo.value)
    assert "拉取模型 qwen2.5:7b 失败" in message
    assert "磁盘空间不足" in message
    assert "需要" in message and "可用" in message
    assert called == []  # 预检失败不得触达 /api/pull


def test_pull_rejects_when_memory_insufficient(monkeypatch):
    _plenty(monkeypatch, mem_free=1 * GB)

    with pytest.raises(model_ops.ModelOpError) as excinfo:
        cli.cmd_pull(
            "qwen2.5:7b",
            "http://ollama",
            transport=_ollama(lambda _r: httpx.Response(200, text="")),
            registry_transport=_registry(10 * GB),
        )

    message = str(excinfo.value)
    assert "内存不足" in message
    assert "需要" in message and "可用" in message


def test_pull_reports_upstream_error(monkeypatch):
    _plenty(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_ndjson([{"error": "pull access denied"}]))

    with pytest.raises(model_ops.ModelOpError) as excinfo:
        cli.cmd_pull(
            "missing:latest",
            "http://ollama",
            transport=_ollama(handler),
            registry_transport=_registry(1024),
        )

    message = str(excinfo.value)
    assert "拉取模型 missing:latest 失败" in message
    assert "pull access denied" in message


def test_pull_reports_http_error(monkeypatch):
    _plenty(monkeypatch)

    with pytest.raises(model_ops.ModelOpError) as excinfo:
        cli.cmd_pull(
            "missing:latest",
            "http://ollama",
            transport=_ollama(
                lambda _r: httpx.Response(500, json={"error": "registry down"})
            ),
            registry_transport=_registry(1024),
        )

    assert "HTTP 500" in str(excinfo.value)
    assert "registry down" in str(excinfo.value)


# ---------------------------------------------------------------- import


def _gguf(tmp_path: Path, name: str = "tiny.gguf", size: int = 2048) -> Path:
    path = tmp_path / name
    path.write_bytes(b"\0" * size)
    return path


def test_import_creates_model_and_prints_modelfile(monkeypatch, tmp_path, capsys):
    _plenty(monkeypatch)
    path = _gguf(tmp_path)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "success"})

    code = cli.cmd_import(
        str(path),
        None,
        "http://ollama",
        system="你是准圣",
        transport=_ollama(handler),
    )

    assert code == 0
    assert captured["path"] == "/api/create"
    payload = captured["payload"]
    assert payload["model"] == "tiny"
    assert payload["from"] == str(path)
    assert payload["system"] == "你是准圣"
    out = capsys.readouterr().out
    assert f"FROM {path}" in out
    assert "已导入模型 tiny" in out


def test_import_rejects_missing_file():
    with pytest.raises(model_ops.ModelOpError) as excinfo:
        cli.cmd_import("/nope/missing.gguf", "m", "http://ollama")
    assert "GGUF 文件不存在" in str(excinfo.value)


def test_import_rejects_when_disk_insufficient(monkeypatch, tmp_path):
    _plenty(monkeypatch, disk_free=1 * 1024 * 1024)
    path = _gguf(tmp_path)
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        return httpx.Response(200, json={"status": "success"})

    with pytest.raises(model_ops.ModelOpError) as excinfo:
        cli.cmd_import(str(path), "m", "http://ollama", transport=_ollama(handler))

    assert "磁盘空间不足" in str(excinfo.value)
    assert called == []


def test_import_reports_create_error(monkeypatch, tmp_path):
    _plenty(monkeypatch)
    path = _gguf(tmp_path)

    with pytest.raises(model_ops.ModelOpError) as excinfo:
        cli.cmd_import(
            str(path),
            "m",
            "http://ollama",
            transport=_ollama(
                lambda _r: httpx.Response(400, json={"error": "invalid GGUF"})
            ),
        )

    message = str(excinfo.value)
    assert "导入模型 m 失败" in message
    assert "invalid GGUF" in message


# ---------------------------------------------------------------- delete


def test_delete_requires_confirmation(monkeypatch, capsys):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200)

    code = cli.cmd_delete(
        "m",
        "http://ollama",
        transport=_ollama(handler),
        confirm=lambda _m: False,
    )

    assert code == 1
    assert calls == []
    assert "已取消删除模型 m" in capsys.readouterr().out


def test_delete_non_interactive_requires_yes():
    with pytest.raises(model_ops.ModelOpError) as excinfo:
        cli.cmd_delete("m", "http://ollama")
    assert "--yes" in str(excinfo.value)


def test_delete_with_yes_skips_confirmation(capsys):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200)

    assert cli.cmd_delete("m", "http://ollama", yes=True, transport=_ollama(handler)) == 0
    assert captured["method"] == "DELETE"
    assert captured["payload"] == {"model": "m"}
    assert "已删除模型 m" in capsys.readouterr().out


def test_delete_reports_upstream_error():
    with pytest.raises(model_ops.ModelOpError) as excinfo:
        cli.cmd_delete(
            "m",
            "http://ollama",
            yes=True,
            transport=_ollama(
                lambda _r: httpx.Response(404, json={"error": "model 'm' not found"})
            ),
        )
    message = str(excinfo.value)
    assert "删除模型 m 失败" in message
    assert "model 'm' not found" in message


# ---------------------------------------------------------------- unload


def test_unload_sends_keep_alive_zero(capsys):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "m", "done": True})

    assert cli.cmd_unload("m", "http://ollama", transport=_ollama(handler)) == 0
    assert captured["path"] == "/api/generate"
    assert captured["payload"] == {"model": "m", "keep_alive": 0}
    assert "已卸载模型 m" in capsys.readouterr().out


def test_unload_reports_upstream_error():
    with pytest.raises(model_ops.ModelOpError) as excinfo:
        cli.cmd_unload(
            "m",
            "http://ollama",
            transport=_ollama(lambda _r: httpx.Response(500, json={"error": "boom"})),
        )
    assert "卸载模型 m 失败" in str(excinfo.value)


# ---------------------------------------------------------------- 其它


def test_remote_model_size_sums_layers():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/library/qwen2.5/manifests/0.5b"
        return httpx.Response(200, json={"layers": [{"size": 10}, {"size": 32}]})

    assert model_ops.remote_model_size(
        "qwen2.5:0.5b", transport=httpx.MockTransport(handler)
    ) == 42


def test_remote_model_size_none_on_error():
    assert (
        model_ops.remote_model_size(
            "x:latest", transport=httpx.MockTransport(lambda _r: httpx.Response(404))
        )
        is None
    )


def test_format_size():
    assert model_ops.format_size(None) == "-"
    assert model_ops.format_size(512) == "512 B"
    assert model_ops.format_size(1024) == "1.0 KB"
    assert model_ops.format_size(630 * 1024 * 1024) == "630.0 MB"


def test_main_model_command_returns_nonzero(monkeypatch, capsys):
    def boom(*_a, **_k):
        raise model_ops.ModelOpError("拉取模型 m 失败：boom")

    monkeypatch.setattr(cli, "cmd_pull", boom)
    monkeypatch.setattr("sys.argv", ["cli", "pull", "m"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    assert "拉取模型 m 失败：boom" in capsys.readouterr().err
