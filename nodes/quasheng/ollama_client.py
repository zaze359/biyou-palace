"""本机 Ollama 客户端：只做代理与元数据读取，**不做语义转换**。

两条硬性约定（来自 research/ollama-openai-compat.md）：
1. 上游请求不带任何 Authorization —— Ollama 忽略 api_key，且我们不转发调用方凭据。
2. 流式响应**原样透传字节**，绝不解析 chunk 内容（delta.content 可能为 None）。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

from .errors import UpstreamUnavailable


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # Ollama 永远跑在本地/回环地址：绝不允许把本机流量路由到环境里的
        # HTTP(S)_PROXY（企业网/沙箱常设），否则绝对 URI 会被代理改写进 path。
        # transport 仅用于测试注入（httpx.MockTransport），生产为 None。
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, trust_env=False, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------- 元数据 / 探活 ----------------

    async def probe(self) -> None:
        """启动探活：确认 Ollama ≥ 0.5.x（存在 /v1/models）。"""
        try:
            resp = await self._client.get("/v1/models")
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(
                f"无法连接 Ollama（{self.base_url}）：{exc}；需要 Ollama ≥ 0.5.x"
            ) from exc
        if resp.status_code >= 400:
            raise UpstreamUnavailable(
                f"Ollama 未暴露 /v1 接口（HTTP {resp.status_code}）；需要 Ollama ≥ 0.5.x"
            )

    async def version(self) -> str | None:
        try:
            resp = await self._client.get("/api/version")
            if resp.status_code == 200:
                return str(resp.json().get("version"))
        except (httpx.HTTPError, ValueError):
            return None
        return None

    async def tags(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/api/tags")
        resp.raise_for_status()
        return list(resp.json().get("models") or [])

    async def ps(self) -> list[dict[str, Any]]:
        """已加载/在跑的模型（用于判断 loaded 与显存占用）。"""
        resp = await self._client.get("/api/ps")
        resp.raise_for_status()
        return list(resp.json().get("models") or [])

    # ---------------- 代理 ----------------

    @asynccontextmanager
    async def stream_post(self, path: str, payload: dict[str, Any]) -> AsyncIterator[httpx.Response]:
        try:
            async with self._client.stream("POST", path, json=payload, headers=self._proxy_headers()) as resp:
                yield resp
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"请求 Ollama 失败：{exc}") from exc

    async def post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        try:
            return await self._client.post(path, json=payload, headers=self._proxy_headers())
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"请求 Ollama 失败：{exc}") from exc

    async def get(self, path: str) -> httpx.Response:
        try:
            return await self._client.get(path, headers=self._proxy_headers())
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"请求 Ollama 失败：{exc}") from exc

    @staticmethod
    def _proxy_headers() -> dict[str, str]:
        # 只带必要头：绝不把调用方的 Authorization 转发给 Ollama
        return {"content-type": "application/json"}
