"""模型生命周期管理：CLI 直连本机 Ollama 原生 `/api/*`。

契约第五章明确节点不提供模型/权重管理 API，因此这些能力只存在于命令行，
不暴露为 HTTP 端点。管理面没有 OpenAI `/v1` 等价接口（`/v1/models` 只读），
必须走 `/api/tags`、`/api/ps`、`/api/pull`、`/api/create`、`/api/delete`、
`/api/generate`。

约定：
- 与 `OllamaClient` 一致使用 `trust_env=False`，本机流量不走 HTTP(S)_PROXY。
- 所有失败抛 `ModelOpError`，消息包含「步骤 + 模型名 + 原因」。
- 拉取/导入前按模型体积做磁盘与内存预检，不足时在真正写入前拒绝。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Iterator

import httpx
import psutil

DEFAULT_OLLAMA_BASE = "http://127.0.0.1:11434"
REGISTRY_BASE = "https://registry.ollama.ai"

# 磁盘预留：除模型本身体积外，给临时 blob/元数据留余量
DISK_RESERVE_BYTES = 512 * 1024 * 1024
# 内存估算：Ollama 加载时除权重外还有 KV cache 与运行时开销，按体积上浮
MEMORY_MULTIPLIER = 1.1


class ModelOpError(Exception):
    """模型管理命令失败（消息已包含步骤、模型与可读原因）。"""


def format_size(size: float | int | None) -> str:
    """把字节数格式化成人类可读单位（与 health 的 MB 风格一致）。"""
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def model_store_dir() -> Path:
    """Ollama 模型落盘目录：优先 OLLAMA_MODELS，否则 ~/.ollama/models。"""
    raw = os.environ.get("OLLAMA_MODELS")
    return Path(raw).expanduser() if raw else Path.home() / ".ollama" / "models"


def _existing_ancestor(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def check_capacity(
    model: str,
    size_bytes: int | None,
    *,
    step: str,
    models_dir: Path | None = None,
) -> None:
    """磁盘与内存预检。体积未知（None）时跳过，由调用方提示。

    磁盘要求：模型体积 + DISK_RESERVE_BYTES；内存要求：体积 × MEMORY_MULTIPLIER。
    不足时抛 `ModelOpError`，消息含所需与可用，便于直接判断差多少。
    """
    if size_bytes is None:
        return

    store = _existing_ancestor(models_dir or model_store_dir())
    disk_free = shutil.disk_usage(store).free
    disk_needed = size_bytes + DISK_RESERVE_BYTES
    if disk_free < disk_needed:
        raise ModelOpError(
            f"{step}模型 {model} 失败：磁盘空间不足，"
            f"需要 {format_size(disk_needed)}（模型 {format_size(size_bytes)} + 预留 "
            f"{format_size(DISK_RESERVE_BYTES)}），可用 {format_size(disk_free)}，"
            f"还差 {format_size(disk_needed - disk_free)}"
        )

    mem_needed = int(size_bytes * MEMORY_MULTIPLIER)
    mem_free = int(psutil.virtual_memory().available)
    if mem_free < mem_needed:
        raise ModelOpError(
            f"{step}模型 {model} 失败：内存不足（按加载后约需 "
            f"{MEMORY_MULTIPLIER:g}× 模型体积估算），"
            f"需要约 {format_size(mem_needed)}，可用 {format_size(mem_free)}，"
            f"还差 {format_size(mem_needed - mem_free)}"
        )


def _registry_path(model: str) -> str:
    name, _, tag = model.partition(":")
    tag = tag or "latest"
    repo = name if "/" in name else f"library/{name}"
    return f"/v2/{repo}/manifests/{tag}"


def remote_model_size(
    model: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 10.0,
) -> int | None:
    """尽力查询 Ollama registry manifest，返回模型层总大小。

    仅用于预检；网络不可达、私有模型或格式未知时返回 None，不阻断拉取。
    """
    try:
        with httpx.Client(
            base_url=REGISTRY_BASE, timeout=timeout, trust_env=False, transport=transport
        ) as client:
            resp = client.get(
                _registry_path(model),
                headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
            )
            if resp.status_code >= 400:
                return None
            payload = resp.json()
    except (httpx.HTTPError, ValueError):
        return None

    layers = payload.get("layers") if isinstance(payload, dict) else None
    if not isinstance(layers, list) or not layers:
        return None
    total = 0
    for layer in layers:
        if isinstance(layer, dict) and isinstance(layer.get("size"), int):
            total += layer["size"]
    return total or None


def build_modelfile(
    gguf_path: Path,
    *,
    system: str | None = None,
    template: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> str:
    """生成等价 Modelfile 文本，便于用户复现同一条 `ollama create`。"""
    lines = [f"FROM {gguf_path}"]
    if system:
        lines.append(f'SYSTEM """{system}"""')
    if template:
        lines.append(f'TEMPLATE """{template}"""')
    for key, value in (parameters or {}).items():
        lines.append(f"PARAMETER {key} {value}")
    return "\n".join(lines) + "\n"


class OllamaAdmin:
    """本机 Ollama 管理端点客户端（同步，供 CLI 使用）。"""

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_BASE,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url, timeout=timeout, trust_env=False, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OllamaAdmin":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------- 基础请求 ----------------

    @staticmethod
    def _reason(resp: httpx.Response) -> str:
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and payload.get("error"):
            return str(payload["error"])
        return (resp.text or "").strip()[:200] or "空响应体"

    def _request(self, method: str, path: str, *, step: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ModelOpError(
                f"{step} 失败：无法连接 Ollama（{self.base_url}）：{exc}"
            ) from exc
        if resp.status_code >= 400:
            raise ModelOpError(f"{step} 失败：HTTP {resp.status_code} · {self._reason(resp)}")
        return resp

    # ---------------- 只读 ----------------

    def tags(self) -> list[dict[str, Any]]:
        resp = self._request("GET", "/api/tags", step="读取已安装模型")
        try:
            return list(resp.json().get("models") or [])
        except ValueError as exc:
            raise ModelOpError("读取已安装模型失败：Ollama 返回了非 JSON 响应") from exc

    def ps(self) -> list[dict[str, Any]]:
        resp = self._request("GET", "/api/ps", step="读取已加载模型")
        try:
            return list(resp.json().get("models") or [])
        except ValueError as exc:
            raise ModelOpError("读取已加载模型失败：Ollama 返回了非 JSON 响应") from exc

    # ---------------- 写入 ----------------

    def pull(self, model: str) -> Iterator[dict[str, Any]]:
        """流式拉取；逐行产出 Ollama 的 NDJSON 事件。"""
        step = f"拉取模型 {model}"
        try:
            with self._client.stream(
                "POST", "/api/pull", json={"model": model, "stream": True}
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise ModelOpError(
                        f"{step} 失败：HTTP {resp.status_code} · {self._reason(resp)}"
                    )
                for line in resp.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        if event.get("error"):
                            raise ModelOpError(f"{step} 失败：{event['error']}")
                        yield event
        except httpx.HTTPError as exc:
            raise ModelOpError(
                f"{step} 失败：无法连接 Ollama（{self.base_url}）：{exc}"
            ) from exc

    def create(
        self,
        model: str,
        gguf_path: Path,
        *,
        system: str | None = None,
        template: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"model": model, "from": str(gguf_path), "stream": False}
        if system:
            payload["system"] = system
        if template:
            payload["template"] = template
        if parameters:
            payload["parameters"] = parameters
        self._request("POST", "/api/create", step=f"导入模型 {model}", json=payload)

    def delete(self, model: str) -> None:
        self._request(
            "DELETE", "/api/delete", step=f"删除模型 {model}", json={"model": model}
        )

    def unload(self, model: str) -> None:
        self._request(
            "POST",
            "/api/generate",
            step=f"卸载模型 {model}",
            json={"model": model, "keep_alive": 0},
        )
