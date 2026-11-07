"""外部 OpenAI 兼容上游（P0 最小桥接）。

硬约束：
- 密钥只保存在节点内存（`Secret`）：不进日志、模型列表、health、错误体、响应头。
- 只使用节点自持密钥；不转发调用方的 `Authorization`。
- 不做动态发现、跨上游回退、聚合或重试（这些属于网关/阵图职责）。
"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

from .config import UpstreamSettings
from .errors import NodeError, normalize_upstream_error

# 主动探活：轻量 GET /models，结果缓存 30s（避免每次 health/models 都打上游）
PROBE_TTL_S = 30.0
PROBE_TIMEOUT_S = 2.0


def scrub_text(text: str, cfg: UpstreamSettings) -> str:
    """从任意文本中抹掉上游密钥与 base_url（错误体/日志唯一入口）。"""
    out = text
    if cfg.api_key:
        out = out.replace(cfg.api_key.get(), "***")
    if cfg.base_url:
        out = out.replace(cfg.base_url, "<upstream>")
    return out


def scrub_payload(value: Any, cfg: UpstreamSettings) -> Any:
    """递归脱敏上游错误体里的字符串，不改变 OpenAI 错误结构。"""
    if isinstance(value, str):
        return scrub_text(value, cfg)
    if isinstance(value, dict):
        return {key: scrub_payload(item, cfg) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_payload(item, cfg) for item in value]
    return value


def _transport_error(cfg: UpstreamSettings, exc: httpx.HTTPError) -> NodeError:
    # 不回显底层异常文本：它可能包含 base_url；密钥也绝不进入错误体
    if isinstance(exc, httpx.TimeoutException):
        return NodeError(
            f"上游 {cfg.id} 请求超时",
            status=504,
            code="upstream_timeout",
            type_="server_error",
        )
    return NodeError(
        f"上游 {cfg.id} 不可达",
        status=503,
        code="upstream_unavailable",
        type_="server_error",
    )


async def read_upstream_error(resp: httpx.Response, cfg: UpstreamSettings) -> dict[str, Any]:
    """把上游错误体归一成 OpenAI 形状并脱敏（状态码由调用方保留）。"""
    try:
        raw = await resp.aread()
    except Exception:  # noqa: BLE001 - 读错误体失败不应盖住原始状态码
        raw = b""
    if cfg.api_key:
        raw = raw.replace(cfg.api_key.get().encode(), b"***")
    if cfg.base_url:
        raw = raw.replace(cfg.base_url.encode(), b"<upstream>")
    return scrub_payload(normalize_upstream_error(resp.status_code, raw), cfg)


def stream_error_frame() -> bytes:
    """流已开始后上游中断：发一帧 SSE error，**不补 [DONE]**。"""
    payload = {
        "error": {
            "message": "上游流中断",
            "type": "server_error",
            "param": None,
            "code": "upstream_stream_aborted",
        }
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


class UpstreamClient:
    """单上游 HTTP 客户端：请求头只带节点自持密钥。"""

    def __init__(
        self,
        cfg: UpstreamSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        headers = {"content-type": "application/json"}
        if cfg.api_key:
            headers["authorization"] = f"Bearer {cfg.api_key.get()}"
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url,
            timeout=httpx.Timeout(cfg.timeout_s, connect=cfg.connect_timeout_s),
            headers=headers,
            # transport 仅用于测试注入（httpx.MockTransport）
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def probe(self) -> tuple[bool, str | None]:
        """轻量探活（不消耗推理配额）。返回 (是否可用, 脱敏错误码)。"""
        try:
            resp = await self._client.get(
                "/models", timeout=min(PROBE_TIMEOUT_S, self.cfg.connect_timeout_s)
            )
        except httpx.TimeoutException:
            return False, "upstream_timeout"
        except httpx.HTTPError:
            return False, "upstream_unavailable"
        if resp.status_code >= 400:
            return False, f"http_{resp.status_code}"
        return True, None

    @asynccontextmanager
    async def stream_chat(self, payload: dict[str, Any]) -> AsyncIterator[httpx.Response]:
        cm = self._client.stream("POST", "/chat/completions", json=payload)
        try:
            resp = await cm.__aenter__()
        except httpx.HTTPError as exc:
            raise _transport_error(self.cfg, exc) from None
        try:
            yield resp
        finally:
            await cm.__aexit__(None, None, None)

    async def post_chat(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            return await self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise _transport_error(self.cfg, exc) from None


class UpstreamRuntime:
    """运行期状态：独立并发名额 + 最近一次探活结果。"""

    def __init__(
        self,
        cfg: UpstreamSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = UpstreamClient(cfg, transport=transport)
        self._active = 0
        self._status = "unknown"
        self._last_error: str | None = None
        self._probed_at = 0.0

    @property
    def active(self) -> int:
        return self._active

    @property
    def status(self) -> str:
        """ready / degraded / down / unknown。"""
        return self._status

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def max_concurrency(self) -> int:
        return self.cfg.max_concurrency

    def try_acquire(self) -> bool:
        """满载立即返回 False（不排队），由路由层转 429。"""
        if self._active >= self.cfg.max_concurrency:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        self._active = max(0, self._active - 1)

    def note_result(self, ok: bool, code: str | None = None) -> None:
        """被动更新状态：请求成功 → ready；传输失败 → down；其余 → degraded。"""
        if ok:
            self._status = "ready"
            self._last_error = None
            return
        self._status = (
            "down" if code in ("upstream_unavailable", "upstream_timeout") else "degraded"
        )
        self._last_error = code

    async def refresh_status(self, force: bool = False) -> str:
        now = time.monotonic()
        if not force and self._probed_at and now - self._probed_at < PROBE_TTL_S:
            return self._status
        ok, error = await self.client.probe()
        self._probed_at = now
        self.note_result(ok, code=error)
        return self._status

    async def aclose(self) -> None:
        await self.client.aclose()
