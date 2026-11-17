"""阵图注册表的出站 reporter（准圣）。

硬约束：
- 只有出站请求，不新增任何入站端点；
- 首次注册在后台任务中执行，**绝不 await 启动流程**；
- 任何失败只改变 reporter 状态与日志，不影响本地推理、health 与节点状态；
- token 只在内存，不进入日志、health 或错误体。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

import httpx

from . import logbuffer
from .config import (
    CONTRACT_VERSION,
    NODE_ROLE,
    Settings,
    Secret,
)

logger = logging.getLogger("quasheng")

SnapshotBuilder = Callable[[], Awaitable[dict[str, Any]]]

# 选路决策硬指标：单次最多等 300ms；成功决策缓存 5s（对应 ttl_ms），
# 失败缓存 10s（沿用 P2，避免阵图宕机时每个请求都多等 300ms）。
ROUTE_QUERY_TIMEOUT_S = 0.3
ROUTE_DECISION_CACHE_TTL_S = 5.0
ROUTE_ERROR_CACHE_TTL_S = 10.0
ROUTE_MAX_CANDIDATES = 10
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")


class RegistryHTTPError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def models_revision(models: list[dict[str, Any]]) -> str:
    """与 Registry/散仙完全一致的模型摘要；loaded 不参与。

    格式：`name|cap1,cap2|context_len` 按 name 排序后用 \\n 连接，再取 SHA-256。
    """
    lines: list[str] = []
    for item in sorted(models, key=lambda entry: str(entry.get("name") or "")):
        caps = ",".join(sorted(set(item.get("capabilities") or [])))
        context = item.get("context_len")
        lines.append(f"{item.get('name')}|{caps}|{'' if context is None else context}")
    raw = "\n".join(lines)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


class RegistryReporter:
    """节点向阵图注册/心跳的后台客户端。"""

    def __init__(
        self,
        settings: Settings,
        snapshot: SnapshotBuilder,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg = settings.registry
        self.settings = settings
        self._snapshot = snapshot
        self._transport = transport
        self._node_id = settings.node_id
        self._boot_id = uuid.uuid4().hex
        self._token = Secret(os.environ.get(self.cfg.token_env) or "")
        self._client: httpx.AsyncClient | None = None
        self._registered = False
        self.state = "disabled" if not self.cfg.enabled else (
            "missing_token" if not self._token else "starting"
        )
        self.last_ok_at: str | None = None
        self.last_error: str | None = None
        self.last_attempt_at: str | None = None
        self.attempts = 0
        self._error_count = 0
        self._last_logged_error: str | None = None

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> asyncio.Task | None:
        if not self.cfg.enabled:
            self.state = "disabled"
            return None
        if not self._token:
            self.state = "missing_token"
            message = (
                f"阵图注册已启用，但环境变量 {self.cfg.token_env} 为空；"
                "注册不会启动（本地服务不受影响）"
            )
            logger.error(message)
            logbuffer.record("ERROR", "registry", message)
            return None
        self.open()
        return asyncio.create_task(self.run(), name=f"registry-{self._node_id}")

    def open(self) -> None:
        """创建出站 HTTP 客户端；测试可直接调用后手动 tick。"""
        if not self.cfg.public_endpoint and self.settings.host in ("0.0.0.0", "::", ""):
            logger.warning(
                "registry.public_endpoint 未配置且监听 %s：注册的 endpoint 是 127.0.0.1，"
                "其他机器不可达；请设置 public_endpoint 为局域网地址",
                self.settings.host,
            )
        headers = {
            "authorization": f"Bearer {self._token.get()}",
            "content-type": "application/json",
            "X-Node-Id": self._node_id,
            "X-Node-Role": NODE_ROLE,
            "Contract-Version": CONTRACT_VERSION,
        }
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.cfg.timeout_s),
            headers=headers,
            transport=self._transport,
            # 注册表是内网旁路，不应被 HTTP(S)_PROXY 改写
            trust_env=False,
        )
        self.state = "starting"

    async def run(self) -> None:
        backoff = 1.0
        # 侧车服务在局域网内：最多按心跳周期退避，保证阵图重启后一个周期内恢复
        max_backoff = float(max(15, min(60, self.cfg.heartbeat_interval_s)))
        while True:
            try:
                await self.tick()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except RegistryHTTPError as exc:
                if exc.status in (404, 409):
                    # 注册表可达，只是不认识当前节点：立即补注册，不沿用宕机退避
                    self._registered = False
                    backoff = 1.0
                self._fail(exc.code)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue
            except Exception as exc:  # noqa: BLE001 - 后台任务异常不得杀死进程
                self._fail(type(exc).__name__)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue
            await asyncio.sleep(self.cfg.heartbeat_interval_s)

    async def tick(self) -> None:
        """执行一轮注册或心跳；测试可直接调用。"""
        self.attempts += 1
        self.last_attempt_at = _now_iso()
        snapshot = await self._snapshot()
        models = list(snapshot.get("models") or [])
        revision = models_revision(models)
        try:
            if not self._registered:
                await self._post("/register", self._register_payload(snapshot, models))
                self._registered = True
            else:
                data = await self._post(
                    f"/{self._node_id}/heartbeat",
                    self._heartbeat_payload(snapshot, models, revision),
                )
                if data.get("need_snapshot"):
                    self._registered = False
        except RegistryHTTPError as exc:
            if exc.status in (404, 409):
                self._registered = False
            raise
        self._ok()

    async def stop(self) -> None:
        client = self._client
        if client is None:
            return
        self._client = None
        if self._registered:
            try:
                await client.post(
                    self._url(f"/{self._node_id}/deregister"),
                    json={"boot_id": self._boot_id},
                    timeout=1.0,
                )
            except Exception:  # noqa: BLE001 - 停机注销尽力而为
                pass
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
        self.state = "stopped"

    # ---------------------------------------------------------------- 状态

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.enabled,
            "state": self.state,
            "registered": self._registered,
            "attempts": self.attempts,
            "last_attempt_at": self.last_attempt_at,
            "last_ok_at": self.last_ok_at,
            "last_error": self.last_error,
        }

    def _ok(self) -> None:
        self.state = "online"
        self.last_ok_at = _now_iso()
        self.last_error = None
        self._error_count = 0
        self._last_logged_error = None

    def _fail(self, code: str) -> None:
        self.state = "offline"
        self.last_error = code
        self._error_count += 1
        # 同一错误限流：首次 + 每 10 次记一条，避免阵图长期宕机刷屏
        if code != self._last_logged_error or self._error_count % 10 == 1:
            message = f"阵图注册异常（{code}），本地服务不受影响"
            logger.warning(message)
            logbuffer.record("WARNING", "registry", message)
            self._last_logged_error = code

    # ---------------------------------------------------------------- HTTP

    def _url(self, path: str) -> str:
        return f"{self.cfg.base_url}/zhentu/v1/nodes{path}"

    def _endpoint(self) -> str:
        if self.cfg.public_endpoint:
            return self.cfg.public_endpoint
        host = self.settings.host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{self.settings.port}"

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._client is not None
        resp = await self._client.post(self._url(path), json=payload)
        if resp.status_code >= 400:
            code = f"http_{resp.status_code}"
            try:
                body = resp.json()
                if isinstance(body, dict) and isinstance(body.get("error"), dict):
                    code = str(body["error"].get("code") or code)
            except ValueError:
                pass
            raise RegistryHTTPError(resp.status_code, code)
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _register_payload(
        self, snapshot: dict[str, Any], models: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "node": snapshot.get("node") or {
                "id": self._node_id,
                "name": self.settings.node_name,
                "role": NODE_ROLE,
                "contract_version": CONTRACT_VERSION,
            },
            "endpoint": self._endpoint(),
            "status": snapshot.get("status") or "down",
            "models": models,
            "limits": snapshot.get("limits") or {},
            "resources": snapshot.get("resources") or {},
            "heartbeat_interval_s": int(self.cfg.heartbeat_interval_s),
            "boot_id": self._boot_id,
            "sent_at": _now_iso(),
        }

    def _heartbeat_payload(
        self, snapshot: dict[str, Any], models: list[dict[str, Any]], revision: str
    ) -> dict[str, Any]:
        return {
            "boot_id": self._boot_id,
            "status": snapshot.get("status") or "down",
            "models_rev": revision,
            "loaded_models": [m["name"] for m in models if m.get("loaded")],
            "resources": snapshot.get("resources") or {},
            "limits": snapshot.get("limits") or {},
            "sent_at": _now_iso(),
        }


@dataclass(frozen=True)
class RouteDecision:
    """一次 `/route` 决策的可用结果；失败时 error 非空、candidates 为空。"""

    decision_id: str | None = None
    candidates: tuple[tuple[str, str | None], ...] = ()
    ttl_ms: int | None = None
    error: str | None = None


def _cache_ttl(decision: RouteDecision) -> float:
    if decision.error:
        return ROUTE_ERROR_CACHE_TTL_S
    ttl_ms = decision.ttl_ms
    if isinstance(ttl_ms, int) and ttl_ms > 0:
        # 尊重阵图给的有效期：阵图调小 TTL 时不能继续用过期决策。
        return min(ROUTE_DECISION_CACHE_TTL_S, ttl_ms / 1000)
    return ROUTE_DECISION_CACHE_TTL_S


def _safe_token(value: Any) -> str | None:
    """只允许可安全放进响应头的字符，避免异常阵图注入响应头。"""
    text = str(value or "").strip()
    return text if _SAFE_TOKEN_RE.fullmatch(text) else None


class RegistryRouteClient:
    """查询阵图单跳选路决策（`POST /zhentu/v1/route`）。

    与注册 reporter 分开：决策延迟直接落在用户请求上，因此使用 300ms 超时；
    成功决策缓存 5s，失败缓存 10s。任何失败都返回空候选，由调用方回退静态
    链条；这里绝不抛异常。
    """

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg = settings.registry
        self._node_id = settings.node_id
        self._token = Secret(os.environ.get(self.cfg.token_env) or "")
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._cache: tuple[float, RouteDecision] | None = None
        self._lock = asyncio.Lock()
        self._last_logged_error: str | None = None

    async def decision(self) -> RouteDecision:
        """返回缓存的决策；错误码非空表示本次查询失败。"""
        now = time.monotonic()
        cached = self._cache
        if cached is not None and now - cached[0] < _cache_ttl(cached[1]):
            return cached[1]
        async with self._lock:
            # 并发请求只让一个去查阵图，其余复用刚写入的缓存
            cached = self._cache
            now = time.monotonic()
            if cached is not None and now - cached[0] < _cache_ttl(cached[1]):
                return cached[1]
            result = await self._fetch()
            self._cache = (now, result)
            return result

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass

    def _open(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(ROUTE_QUERY_TIMEOUT_S),
                headers={
                    "authorization": f"Bearer {self._token.get()}",
                    "X-Node-Id": self._node_id,
                    "X-Node-Role": NODE_ROLE,
                    "Contract-Version": CONTRACT_VERSION,
                },
                transport=self._transport,
                # 阵图是内网旁路，不应被 HTTP(S)_PROXY 改写
                trust_env=False,
            )
        return self._client

    async def _fetch(self) -> RouteDecision:
        if not self.cfg.enabled:
            return RouteDecision(error="disabled")
        if not self._token:
            self._warn("missing_token")
            return RouteDecision(error="missing_token")
        payload = {
            "capability": "chat",
            "max_candidates": ROUTE_MAX_CANDIDATES,
            # 与 P2 的候选查询一致：不把本节点当作自己的回退
            "exclude_nodes": [self._node_id],
        }
        try:
            resp = await self._open().post(
                f"{self.cfg.base_url}/zhentu/v1/route", json=payload
            )
        except Exception as exc:  # noqa: BLE001 - 阵图失败不得影响请求
            code = type(exc).__name__
            self._warn(code)
            return RouteDecision(error=code)
        if resp.status_code >= 400:
            code = f"http_{resp.status_code}"
            self._warn(code)
            return RouteDecision(error=code)
        try:
            data = resp.json()
        except ValueError:
            self._warn("bad_json")
            return RouteDecision(error="bad_json")
        if not isinstance(data, dict):
            self._warn("bad_json")
            return RouteDecision(error="bad_json")

        decision_id = _safe_token(data.get("decision_id"))
        if decision_id is None:
            # 没有决策标识说明响应形状不对，按失败处理，避免静默吞错。
            self._warn("bad_json")
            return RouteDecision(error="bad_json")

        alternates = data.get("alternates")
        entries = [data.get("selected"), *(alternates if isinstance(alternates, list) else [])]
        candidates: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for item in entries:
            if not isinstance(item, dict):
                continue
            name = str(item.get("model") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            candidates.append((name, _safe_token(item.get("node_id"))))
        ttl_raw = data.get("ttl_ms")
        ttl_ms = (
            ttl_raw
            if isinstance(ttl_raw, int) and not isinstance(ttl_raw, bool) and ttl_raw > 0
            else None
        )
        self._last_logged_error = None
        return RouteDecision(
            decision_id=decision_id,
            candidates=tuple(candidates),
            ttl_ms=ttl_ms,
        )

    def _warn(self, code: str) -> None:
        # 同一错误只记一次；恢复后重置，避免阵图长期宕机刷屏
        if code == self._last_logged_error:
            return
        self._last_logged_error = code
        message = f"阵图路由查询失败（{code}），本次回退静态链条"
        logger.warning(message)
        logbuffer.record("WARNING", "fallback", message)

