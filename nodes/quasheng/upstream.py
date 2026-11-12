"""外部 OpenAI 兼容上游（P0 最小桥接 + P1 可选动态发现）。

硬约束：
- 密钥只保存在节点内存（`Secret`）：不进日志、模型列表、health、错误体、响应头。
- 只使用节点自持密钥；不转发调用方的 `Authorization`。
- 动态发现是节点自身行为：只读上游 `/v1/models`，不新增任何对外管理端点。
- 不做跨上游回退、聚合或重试（这些属于网关/阵图职责）。
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator

import httpx

from . import logbuffer
from .config import UpstreamModel, UpstreamSettings
from .errors import NodeError, normalize_upstream_error

logger = logging.getLogger("quasheng")

# 主动探活：轻量 GET /models，结果缓存 30s（避免每次 health/models 都打上游）
PROBE_TTL_S = 30.0
PROBE_TIMEOUT_S = 2.0
# 动态发现也是只读 GET /models；给比探活稍宽的窗口，失败不阻塞启动
DISCOVERY_TIMEOUT_S = 3.0


class DiscoveryError(Exception):
    """模型发现失败（code 为脱敏后的短原因，可直接进 health）。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code



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

    async def list_models(self) -> list[str]:
        """读取上游 `/v1/models` 的模型 id 列表（只读，用于动态发现）。

        失败时抛 `DiscoveryError(code)`；code 不含 base_url / 密钥。
        `data: []` 是合法成功结果（表示上游当前没有模型）。
        """
        try:
            resp = await self._client.get(
                "/models", timeout=min(DISCOVERY_TIMEOUT_S, self.cfg.connect_timeout_s)
            )
        except httpx.TimeoutException:
            raise DiscoveryError("upstream_timeout") from None
        except httpx.HTTPError:
            raise DiscoveryError("upstream_unavailable") from None
        if resp.status_code >= 400:
            raise DiscoveryError(f"http_{resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            raise DiscoveryError("bad_response") from None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise DiscoveryError("bad_response")
        ids: list[str] = []
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
        return ids

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
        # 动态发现快照：discover=false 时恒等于静态白名单
        self._models: list[UpstreamModel] = list(cfg.models)
        self._last_discovery_at: str | None = None
        self._discovery_error: str | None = None

    @property
    def active(self) -> int:
        return self._active

    @property
    def models(self) -> list[UpstreamModel]:
        """当前对外可路由的远程模型（静态清单或最近一次发现快照）。"""
        return self._models

    def model_for(self, model_id: str) -> UpstreamModel | None:
        for item in self._models:
            if item.id == model_id:
                return item
        return None

    @property
    def last_discovery_at(self) -> str | None:
        """最近一次发现成功的时间（ISO 本地时区）；从未成功为 None。"""
        return self._last_discovery_at

    @property
    def discovery_error(self) -> str | None:
        """最近一次发现失败原因（脱敏短码）；成功后清空。"""
        return self._discovery_error

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

    def _intersect(self, ids: list[str]) -> list[UpstreamModel]:
        """发现结果与白名单取交集；白名单为空时全部放行。

        - 有白名单：按白名单顺序保留同时被发现的项，能力/上下文沿用白名单。
        - 无白名单：按上游返回顺序去重，能力默认 chat、上下文未知。
        """
        if self.cfg.models:
            discovered = set(ids)
            return [model for model in self.cfg.models if model.id in discovered]

        out: list[UpstreamModel] = []
        seen: set[str] = set()
        for model_id in ids:
            if model_id in seen:
                continue
            seen.add(model_id)
            out.append(UpstreamModel(id=model_id))
        return out

    async def discover(self) -> bool:
        """拉取上游 /v1/models 并更新快照。

        失败时**保留上一次成功的快照**，只记录错误与时间；返回是否成功。
        不修改状态机（探活/请求结果仍由 note_result 决定）。
        """
        cfg = self.cfg
        try:
            ids = await self.client.list_models()
        except DiscoveryError as exc:
            self._discovery_error = exc.code
            snapshot = self._last_discovery_at or "尚无成功快照"
            logger.warning(
                "上游 %s 模型发现失败：%s，沿用上次快照（%s）", cfg.id, exc.code, snapshot
            )
            logbuffer.record(
                "WARNING",
                "upstream",
                f"上游 {cfg.id} 模型发现失败：{exc.code}，沿用上次快照（{snapshot}）",
            )
            return False

        self._models = self._intersect(ids)
        self._last_discovery_at = datetime.now().astimezone().isoformat()
        self._discovery_error = None
        if ids:
            logger.info(
                "上游 %s 发现 %d 个模型，交集后对外 %d 个",
                cfg.id,
                len(ids),
                len(self._models),
            )
            logbuffer.record(
                "INFO",
                "upstream",
                f"上游 {cfg.id} 发现 {len(ids)} 个模型，交集后对外 {len(self._models)} 个",
            )
        else:
            # 200 + 空列表是合法快照，但多半意味着上游没配置模型，值得告警
            logger.warning("上游 %s 返回空模型列表，当前对外远程模型为 0 个", cfg.id)
            logbuffer.record(
                "WARNING", "upstream", f"上游 {cfg.id} 返回空模型列表，当前对外远程模型为 0 个"
            )
        return True

    async def aclose(self) -> None:
        await self.client.aclose()
