"""准圣节点智能体 · FastAPI 装配。

职责边界（薄代理 + 契约专属表面）：
- 只做切面：鉴权 / 响应头 / 并发闸门 / 断连取消
- OpenAI 语义：反向代理 Ollama /v1/*，SSE **原样透传**
- 原生实现：/v1/health（tags + ps + psutil 拼装）
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import CONTRACT_VERSION, NODE_ROLE, Settings, load_settings
from .concurrency import ConcurrencyGate
from .errors import (
    ConcurrencyLimitExceeded,
    NodeError,
    normalize_upstream_error,
    openai_error,
    openai_error_payload,
)
from .health import build_health
from .ollama_client import OllamaClient

logger = logging.getLogger("quasheng")


def _configure_logging() -> None:
    """让 quasheng 的 INFO 日志在 uvicorn 启动方式下可见。

    uvicorn 只配置自己的 logger，第三方 logger 的 INFO 会被 lastResort
    丢弃（只输出 WARNING+）。这里给 quasheng 挂独立 handler，同时保留
    向 root 的传播，方便 pytest caplog 捕获。
    """
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [quasheng] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------- 切面


async def require_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """bearer 鉴权，覆盖全部端点（含 /v1/health）。

    注意：这里校验的 token 只用于本节点，**不会**转发给 Ollama
    （Ollama 忽略 api_key，且转发调用方凭据是多余的暴露面）。
    """
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled or not settings.shared_token:
        return
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or token != settings.shared_token:
        raise NodeError(
            "缺少或无效的 bearer token",
            status=401,
            code="invalid_api_key",
            type_="invalid_request_error",
        )


def _passthrough(resp) -> JSONResponse:
    """上游非流式响应原样透传（含上游自己的 OpenAI 错误体）。"""
    try:
        body = resp.json()
    except ValueError:
        body = {
            "error": {
                "message": resp.text[:500],
                "type": "server_error",
                "param": None,
                "code": "upstream_bad_response",
            }
        }
    return JSONResponse(status_code=resp.status_code, content=body)


async def _read_upstream_error(upstream) -> dict[str, Any]:
    """把上游错误体归一成 OpenAI 风格（状态码由调用方保留）。"""
    try:
        raw = await upstream.aread()
    except Exception:  # noqa: BLE001 - 读错误体失败不应盖住原始状态码
        raw = b""
    return normalize_upstream_error(upstream.status_code, raw)


async def _stream_body(request: Request, upstream, stack: AsyncExitStack, gate: ConcurrencyGate):
    """SSE 流式透传：**不解析 chunk**，客户端断开即中止上游生成。"""
    try:
        async for chunk in upstream.aiter_bytes():
            if await request.is_disconnected():
                logger.info("客户端断开，中止上游生成")
                break
            yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        # Starlette 在客户端断开时会取消流任务；显式断连检查可能来不及触发。
        logger.info("客户端断开，中止上游生成")
        raise
    finally:
        await stack.aclose()
        await gate.release()


# ---------------------------------------------------------------- 预热


async def _warmup(client: OllamaClient, settings: Settings) -> None:
    """预热白名单模型使其常驻，消除"首次请求阻塞 30s+"。

    首选 /api/generate（keep_alive=-1 常驻）；若该 Ollama 版本拒绝空
    prompt，退化为一次 num_predict=1 的最小对话。以实测为准。
    """
    if not settings.preload_models:
        return
    for model in settings.preload_models:
        started = time.perf_counter()
        path = "/api/generate"
        try:
            resp = await client.post(path, {"model": model, "prompt": " ", "keep_alive": -1})
            if resp.status_code >= 400:
                path = "/api/chat"
                resp = await client.post(
                    path,
                    {
                        "model": model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": False,
                        "keep_alive": -1,
                        "options": {"num_predict": 1},
                    },
                )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if resp.status_code >= 400:
                logger.warning(
                    "预热模型 %s 失败：路径=%s HTTP %s，耗时 %d ms",
                    model,
                    path,
                    resp.status_code,
                    elapsed_ms,
                )
            else:
                logger.info("预热模型 %s 完成：路径=%s，耗时 %d ms", model, path, elapsed_ms)
        except Exception as exc:  # noqa: BLE001 - 预热失败不阻断启动
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.warning("预热模型 %s 异常：路径=%s，耗时 %d ms，%s", model, path, elapsed_ms, exc)


# ---------------------------------------------------------------- 生命周期


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    client = OllamaClient(settings.ollama_base, settings.request_timeout)
    app.state.settings = settings
    app.state.client = client
    app.state.gate = ConcurrencyGate(settings.max_concurrency)

    try:
        await client.probe()  # 版本强约束：Ollama ≥ 0.5.x，否则启动即失败
    except NodeError as exc:
        logger.error("启动失败：%s", exc.message)
        raise
    logger.info(
        "准圣节点启动：%s（%s）→ Ollama %s", settings.node_id, settings.node_name, settings.ollama_base
    )
    await _warmup(client, settings)

    yield
    await client.aclose()


def _node_headers(request: Request) -> dict[str, str]:
    settings: Settings | None = getattr(request.app.state, "settings", None)
    return {
        "X-Node-Id": settings.node_id if settings else "unknown",
        "X-Node-Role": NODE_ROLE,
        "Contract-Version": CONTRACT_VERSION,
    }


def create_app() -> FastAPI:
    _configure_logging()
    app = FastAPI(
        title="BiYouPalace 准圣节点智能体",
        version=CONTRACT_VERSION,
        lifespan=lifespan,
        dependencies=[Depends(require_auth)],
    )

    @app.middleware("http")
    async def add_node_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(_node_headers(request))
        return response

    @app.exception_handler(NodeError)
    async def handle_node_error(request: Request, exc: NodeError):
        return openai_error(exc.message, status=exc.status, type_=exc.type_, code=exc.code)

    @app.exception_handler(ConcurrencyLimitExceeded)
    async def handle_concurrency(request: Request, exc: ConcurrencyLimitExceeded):
        return openai_error(
            exc.message,
            status=exc.status,
            type_=exc.type_,
            code=exc.code,
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        first = (exc.errors() or [{}])[0]
        message = str(first.get("msg") or "请求体格式错误")
        return JSONResponse(
            status_code=422,
            content=openai_error_payload(message, code="invalid_request_error"),
            headers=_node_headers(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        type_ = "invalid_request_error" if exc.status_code < 500 else "server_error"
        return JSONResponse(
            status_code=exc.status_code,
            content=openai_error_payload(str(exc.detail), type_=type_, code=code),
            headers=dict(exc.headers or {}, **_node_headers(request)),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        # 500 由 ServerErrorMiddleware 直接返回，绕过了自定义 http 中间件，
        # 因此这里显式补上 X-Node-* 响应头。
        logger.exception("未捕获异常：%s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=openai_error_payload("节点内部错误", type_="server_error", code="internal_error"),
            headers=_node_headers(request),
        )

    @app.get("/v1/health")
    async def health(request: Request):
        return await build_health(
            request.app.state.settings, request.app.state.client, request.app.state.gate
        )

    @app.get("/v1/models")
    async def models(request: Request):
        resp = await request.app.state.client.get("/v1/models")
        return _passthrough(resp)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, payload: dict[str, Any]):
        client: OllamaClient = request.app.state.client
        gate: ConcurrencyGate = request.app.state.gate

        if bool(payload.get("stream")):
            # 先抢占名额（满了立刻 429），名额**由流结束时**释放——
            # 不能套 async with，否则返回 StreamingResponse 就释放了。
            await gate.acquire()
            stack = AsyncExitStack()
            try:
                upstream = await stack.enter_async_context(
                    client.stream_post("/v1/chat/completions", payload)
                )
            except BaseException:
                await stack.aclose()
                await gate.release()
                raise

            # 必须先拿到上游响应头再决定回什么：上游 4xx/5xx 时不能包装成
            # 200 + SSE 正文（那样调用方会以为流已开始，错误体被当成 chunk）。
            if upstream.status_code >= 400:
                body = await _read_upstream_error(upstream)
                await stack.aclose()
                await gate.release()
                return JSONResponse(status_code=upstream.status_code, content=body)

            return StreamingResponse(
                _stream_body(request, upstream, stack, gate), media_type="text/event-stream"
            )

        async with gate.slot():
            resp = await client.post("/v1/chat/completions", payload)
            return _passthrough(resp)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request, payload: dict[str, Any]):
        settings: Settings = request.app.state.settings
        client: OllamaClient = request.app.state.client
        gate: ConcurrencyGate = request.app.state.gate

        model = str(payload.get("model") or "")
        if "embedding" not in settings.capability_for(model):
            raise NodeError(
                f"模型 {model or '(未指定)'} 未声明 embedding 能力",
                status=501,
                code="capability_not_supported",
                type_="invalid_request_error",
            )

        async with gate.slot():
            resp = await client.post("/v1/embeddings", payload)
            return _passthrough(resp)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run("nodes.quasheng.app:app", host="0.0.0.0", port=load_settings().port)
