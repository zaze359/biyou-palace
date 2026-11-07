"""准圣节点智能体 · FastAPI 装配。

职责边界（薄代理 + 契约专属表面）：
- 只做切面：鉴权 / 响应头 / 并发闸门 / 断连取消
- OpenAI 语义：反向代理 Ollama /v1/*，SSE **原样透传**
- 原生实现：/v1/health（tags + ps + psutil 拼装）
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import logbuffer
from .config import (
    CONTRACT_VERSION,
    MODEL_OWNER,
    NODE_ROLE,
    ConfigError,
    Settings,
    UpstreamModel,
    UpstreamSettings,
    check_local_model_conflicts,
    is_loopback,
    load_settings,
)
from .concurrency import ConcurrencyGate
from .loading import LoadingTracker
from .errors import (
    ConcurrencyLimitExceeded,
    NodeError,
    normalize_upstream_error,
    openai_error,
    openai_error_payload,
)
from .health import build_health
from .ollama_client import OllamaClient
from .upstream import (
    UpstreamRuntime,
    read_upstream_error,
    scrub_payload,
    scrub_text,
    stream_error_frame,
)

logger = logging.getLogger("quasheng")

# 控制面板静态资源目录（单文件 HTML，由 /ui 挂载；不经过 API 鉴权）
_UI_DIR = Path(__file__).parent / "static"

# 启动探针：有限次数 + 指数退避。Ollama 可能在开机时晚于节点就绪，
# 立即退出会让 launchd/systemd 进入无效重启循环；重试窗口内恢复即可。
PROBE_ATTEMPTS = 5
PROBE_BASE_DELAY = 1.0


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
        # 只记录“失败”这一事实，不记录调用方提供的 token 值
        logbuffer.record("WARNING", "auth", "鉴权失败：缺少或无效的 bearer token")
        raise NodeError(
            "缺少或无效的 bearer token",
            status=401,
            code="invalid_api_key",
            type_="invalid_request_error",
        )


def _record_upstream_error(status: int, body: Any, provider: str | None = None) -> None:
    """把上游错误收敛成一条脱敏事件（不写请求头/请求体）。"""
    code = "unknown"
    message = ""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        code = str(body["error"].get("code") or code)
        message = str(body["error"].get("message") or "")
    tail = f" · {message[:120]}" if message else ""
    prefix = f"上游 {provider} " if provider else "上游"
    logbuffer.record(
        "ERROR" if status >= 500 else "WARNING",
        "upstream",
        f"{prefix}错误：HTTP {status} · {code}{tail}",
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
    if resp.status_code >= 400:
        _record_upstream_error(resp.status_code, body)
    return JSONResponse(status_code=resp.status_code, content=body)


def _models_response(resp, extra_models: list[dict[str, Any]] | None = None) -> JSONResponse:
    """`/v1/models` 唯一需要改写上游 JSON 的端点。

    契约 3.2 要求每项 `owned_by` 为节点身份（biyou-node），而 Ollama 返回
    `library`。其余字段原样透传，保持薄代理。`extra_models` 为远程模型项
    （已按可见性过滤），追加在本地模型之后。
    """
    try:
        body = resp.json()
    except ValueError:
        return _passthrough(resp)
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        for item in body["data"]:
            if isinstance(item, dict):
                item["owned_by"] = MODEL_OWNER
        if extra_models:
            body["data"].extend(extra_models)
    return JSONResponse(status_code=resp.status_code, content=body)


async def _read_upstream_error(upstream) -> dict[str, Any]:
    """把上游错误体归一成 OpenAI 风格（状态码由调用方保留）。"""
    try:
        raw = await upstream.aread()
    except Exception:  # noqa: BLE001 - 读错误体失败不应盖住原始状态码
        raw = b""
    return normalize_upstream_error(upstream.status_code, raw)


async def _stream_body(
    request: Request,
    upstream,
    stack: AsyncExitStack,
    gate: ConcurrencyGate,
    *,
    loading: LoadingTracker | None = None,
    model: str | None = None,
):
    """SSE 流式透传：**不解析 chunk**，客户端断开即中止上游生成。

    首个 chunk 到达意味着模型已完成加载，清除 loading；若流在首个 chunk
    前结束/断开，则视为加载失败，同样清除 pending。
    """
    loaded_seen = False
    try:
        async for chunk in upstream.aiter_bytes():
            if not loaded_seen:
                loaded_seen = True
                if loading is not None and model:
                    loading.mark_loaded(model)
            if await request.is_disconnected():
                logger.info("客户端断开，中止上游生成")
                logbuffer.record("WARNING", "disconnect", "客户端断开，中止上游生成")
                break
            yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        # Starlette 在客户端断开时会取消流任务；显式断连检查可能来不及触发。
        logger.info("客户端断开，中止上游生成")
        logbuffer.record("WARNING", "disconnect", "客户端断开，中止上游生成")
        raise
    finally:
        if not loaded_seen and loading is not None and model:
            loading.mark_failed(model)
        await stack.aclose()
        await gate.release()


# ---------------------------------------------------------------- 外部上游桥接


def _upstream_runtimes(request: Request) -> list[UpstreamRuntime]:
    return list(getattr(request.app.state, "upstreams", []) or [])


def _source_is_loopback(request: Request) -> bool:
    """来源是否回环：`visibility=local` 的远程模型只允许本机调用。

    FastAPI TestClient 的默认来源是 `testclient`，与回环等价。
    """
    host = request.client.host if request.client else None
    return bool(host) and (host == "testclient" or is_loopback(host))


def _resolve_remote(
    settings: Settings, model: str
) -> tuple[UpstreamSettings | None, UpstreamModel | None]:
    """解析 `<provider>/<model>`：返回 (上游配置, 静态清单项)。

    - 不认识的 provider / 不带斜杠的裸名 → (None, None)，维持原本地路径。
    - 已知 provider 但模型未在清单 → (cfg, None)，由调用方统一 404。
    """
    if "/" not in model:
        return None, None
    provider, _, rest = model.partition("/")
    for cfg in settings.upstreams:
        if cfg.enabled and cfg.id == provider:
            return cfg, cfg.model_for(rest)
    return None, None


def _runtime_for(request: Request, cfg: UpstreamSettings) -> UpstreamRuntime | None:
    for runtime in _upstream_runtimes(request):
        if runtime.cfg.id == cfg.id:
            return runtime
    return None


def _model_not_found(model: str) -> NodeError:
    # 未知与未授权统一 404，不泄漏“该模型是否存在”
    return NodeError(
        f"模型 {model or '(未指定)'} 不存在或当前调用方无权使用",
        status=404,
        code="model_not_found",
        type_="invalid_request_error",
    )


def _remote_visible(request: Request, cfg: UpstreamSettings) -> bool:
    if cfg.visibility in ("cluster", "both"):
        return True
    return _source_is_loopback(request)


def _error_code(body: Any) -> str:
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return str(body["error"].get("code") or "upstream_error")
    return "upstream_error"


def _upstream_error_response(
    status: int,
    body: dict[str, Any],
    cfg: UpstreamSettings,
    *,
    retry_after: str | None = None,
) -> JSONResponse:
    headers = {"Retry-After": retry_after} if retry_after else None
    _record_upstream_error(status, body, cfg.id)
    return JSONResponse(status_code=status, content=body, headers=headers)


def _remote_health_models(runtime: UpstreamRuntime) -> list[dict[str, Any]]:
    cfg = runtime.cfg
    status = runtime.status
    return [
        {
            "name": f"{cfg.id}/{model.id}",
            "family": None,
            "size_label": None,
            "context_len": model.context_len,
            "loaded": False,
            "capabilities": list(model.capabilities),
            "provider": cfg.id,
            "source": "upstream",
            "available": status == "ready",
            "upstream_status": status,
            "data_leaves_lan": cfg.data_leaves_lan,
        }
        for model in cfg.models
    ]


def _remote_list_entries(runtime: UpstreamRuntime) -> list[dict[str, Any]]:
    cfg = runtime.cfg
    status = runtime.status
    return [
        {
            "id": f"{cfg.id}/{model.id}",
            "object": "model",
            "owned_by": MODEL_OWNER,
            "provider": cfg.id,
            "source": "upstream",
            "available": status == "ready",
        }
        for model in cfg.models
    ]


async def _stream_upstream_body(
    request: Request,
    upstream,
    stack: AsyncExitStack,
    runtime: UpstreamRuntime,
    gate: ConcurrencyGate,
):
    """SSE 原样透传；流开始后上游中断发 SSE error 帧，**不补 [DONE]**。"""
    try:
        async for chunk in upstream.aiter_bytes():
            if await request.is_disconnected():
                logger.info("客户端断开，中止上游 %s 生成", runtime.cfg.id)
                logbuffer.record("WARNING", "disconnect", "客户端断开，中止上游生成")
                break
            yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        logbuffer.record("WARNING", "disconnect", "客户端断开，中止上游生成")
        raise
    except Exception:  # noqa: BLE001 - 流已开始，不能改回 JSON 错误
        runtime.note_result(False, code="upstream_stream_aborted")
        logger.warning("上游 %s 流中断", runtime.cfg.id)
        logbuffer.record(
            "WARNING", "upstream", f"上游 {runtime.cfg.id} 流中断：upstream_stream_aborted"
        )
        yield stream_error_frame()
    finally:
        await stack.aclose()
        runtime.release()
        await gate.release()


async def _handle_upstream_chat(
    request: Request,
    payload: dict[str, Any],
    runtime: UpstreamRuntime,
    gate: ConcurrencyGate,
    remote_model: UpstreamModel,
) -> Any:
    cfg = runtime.cfg
    # 只把 model 换成上游原生 id；其余字段（messages/stream/参数）原样透传
    forward = {**payload, "model": remote_model.id}

    # 全局闸门仍是节点级上限；上游信号量是第二层，满载立即 429、不排队
    def _capacity_error() -> JSONResponse:
        logbuffer.record(
            "WARNING",
            "upstream",
            f"上游 {cfg.id} 并发拒绝：已达上限 {cfg.max_concurrency}，返回 429",
        )
        return openai_error(
            f"上游 {cfg.id} 并发已达上限 {cfg.max_concurrency}，请稍后重试",
            status=429,
            type_="rate_limit_error",
            code="upstream_capacity",
            headers={"Retry-After": "1"},
        )

    if bool(payload.get("stream")):
        await gate.acquire()
        if not runtime.try_acquire():
            await gate.release()
            return _capacity_error()
        stack = AsyncExitStack()
        try:
            upstream = await stack.enter_async_context(runtime.client.stream_chat(forward))
        except BaseException as exc:
            if isinstance(exc, NodeError):
                runtime.note_result(False, code=exc.code)
            runtime.release()
            await stack.aclose()
            await gate.release()
            raise

        # 上游 4xx/5xx 必须按原状态码返回 JSON 错误，不能包装成 200 + SSE
        if upstream.status_code >= 400:
            body = await read_upstream_error(upstream, cfg)
            runtime.note_result(False, code=_error_code(body))
            await stack.aclose()
            runtime.release()
            await gate.release()
            return _upstream_error_response(
                upstream.status_code,
                body,
                cfg,
                retry_after=upstream.headers.get("retry-after") if upstream.status_code == 429 else None,
            )

        runtime.note_result(True)
        return StreamingResponse(
            _stream_upstream_body(request, upstream, stack, runtime, gate),
            media_type="text/event-stream",
        )

    async with gate.slot():
        if not runtime.try_acquire():
            return _capacity_error()
        try:
            resp = await runtime.client.post_chat(forward)
        except NodeError as exc:
            runtime.note_result(False, code=exc.code)
            raise
        finally:
            runtime.release()

        if resp.status_code >= 400:
            body = await read_upstream_error(resp, cfg)
            runtime.note_result(False, code=_error_code(body))
            return _upstream_error_response(
                resp.status_code,
                body,
                cfg,
                retry_after=resp.headers.get("retry-after") if resp.status_code == 429 else None,
            )

        runtime.note_result(True)
        try:
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except ValueError:
            # 上游 2xx 但非 JSON：保持薄代理语义，按原状态码回传
            return JSONResponse(
                status_code=resp.status_code,
                content={
                    "error": {
                        "message": scrub_text(resp.text[:500], cfg),
                        "type": "server_error",
                        "param": None,
                        "code": "upstream_bad_response",
                    }
                },
            )


# ---------------------------------------------------------------- 预热与能力


def _require_capability(settings: Settings, model: str, capability: str) -> None:
    """能力校验：未声明该能力的模型直接 501，由上层改路由（契约 3.3）。"""
    if capability not in settings.capability_for(model):
        raise NodeError(
            f"模型 {model or '(未指定)'} 未声明 {capability} 能力",
            status=501,
            code="capability_not_supported",
            type_="invalid_request_error",
        )


async def _warmup(
    client: OllamaClient,
    settings: Settings,
    loading: LoadingTracker | None = None,
    failed: set[str] | None = None,
) -> None:
    """预热白名单模型使其常驻，消除"首次请求阻塞 30s+"。

    首选 /api/generate（keep_alive=-1 常驻）；若该 Ollama 版本拒绝空
    prompt，退化为一次 num_predict=1 的最小对话。以实测为准。
    预热失败的模型记入 `failed`，health 不再把它算作 pending loading。
    """
    if not settings.preload_models:
        return
    for model in settings.preload_models:
        if loading is not None:
            loading.note_request(model)
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
                logbuffer.record(
                    "WARNING", "warmup", f"预热 {model} 失败：{path} HTTP {resp.status_code}，{elapsed_ms} ms"
                )
                if loading is not None:
                    loading.mark_failed(model)
                if failed is not None:
                    failed.add(model)
            else:
                logger.info("预热模型 %s 完成：路径=%s，耗时 %d ms", model, path, elapsed_ms)
                logbuffer.record("INFO", "warmup", f"预热 {model} 完成：{path}，{elapsed_ms} ms")
                if loading is not None:
                    loading.mark_loaded(model)
        except Exception as exc:  # noqa: BLE001 - 预热失败不阻断启动
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.warning("预热模型 %s 异常：路径=%s，耗时 %d ms，%s", model, path, elapsed_ms, exc)
            logbuffer.record(
                "WARNING", "warmup", f"预热 {model} 异常：{path}，{elapsed_ms} ms，{exc}"
            )
            if loading is not None:
                loading.mark_failed(model)
            if failed is not None:
                failed.add(model)


# ---------------------------------------------------------------- 生命周期


async def _probe_with_retry(
    client: OllamaClient,
    attempts: int | None = None,
    base_delay: float | None = None,
) -> None:
    """启动探针：有限次数指数退避，给 Ollama 留出开机慢启动的窗口。"""
    total = max(1, PROBE_ATTEMPTS if attempts is None else int(attempts))
    delay = PROBE_BASE_DELAY if base_delay is None else max(0.0, float(base_delay))
    last: NodeError | None = None
    for index in range(total):
        try:
            await client.probe()  # 版本强约束：Ollama ≥ 0.5.x
            return
        except NodeError as exc:
            last = exc
            if index < total - 1:
                wait = delay * (2 ** index)
                logger.warning(
                    "Ollama 未就绪（第 %d/%d 次）：%s；%.1fs 后重试",
                    index + 1,
                    total,
                    exc.message,
                    wait,
                )
                logbuffer.record(
                    "WARNING",
                    "startup",
                    f"Ollama 未就绪（第 {index + 1}/{total} 次）：{exc.message}；{wait:.1f}s 后重试",
                )
                await asyncio.sleep(wait)
    assert last is not None
    raise last


@asynccontextmanager
async def lifespan(app: FastAPI):
    logbuffer.configure(None)  # 先建缓冲，配置错误也要能记录
    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error("配置错误，拒绝启动：%s", exc)
        logbuffer.record("ERROR", "startup", f"配置错误：{exc}")
        raise
    client = OllamaClient(settings.ollama_base, settings.request_timeout)
    app.state.settings = settings
    app.state.client = client
    app.state.gate = ConcurrencyGate(settings.max_concurrency)
    app.state.loading = LoadingTracker(settings.request_timeout)
    app.state.warmup_failed = set()
    app.state.shutting_down = False
    app.state.upstreams = [UpstreamRuntime(cfg) for cfg in settings.upstreams if cfg.enabled]
    logbuffer.configure(settings.shared_token)  # 脱敏用的 token，仅内存

    for cfg in settings.upstreams:
        if not cfg.enabled:
            logger.info("上游 %s 未启用（enabled: false），已跳过", cfg.id)
            logbuffer.record("INFO", "upstream", f"上游 {cfg.id} 未启用（enabled: false），已跳过")
            continue
        if cfg.base_url.startswith("http://"):
            logger.warning("上游 %s 使用明文 http，请确认它只在内网/回环", cfg.id)
            logbuffer.record("WARNING", "upstream", f"上游 {cfg.id} 使用明文 http（仅建议内网/回环）")
        if cfg.data_leaves_lan:
            level = "WARNING" if cfg.cluster_visible() else "INFO"
            logger.log(
                logging.WARNING if level == "WARNING" else logging.INFO,
                "上游 %s 会把 prompt 送出局域网（visibility=%s）",
                cfg.id,
                cfg.visibility,
            )
            logbuffer.record(
                level,
                "upstream",
                f"上游 {cfg.id} 会把 prompt 送出局域网（visibility={cfg.visibility}）",
            )

    try:
        await _probe_with_retry(client)
    except NodeError as exc:
        logger.error("启动失败：%s", exc.message)
        logbuffer.record("ERROR", "startup", f"启动失败：{exc.message}")
        raise

    # 启动期 id 唯一性校验：本地模型首段不得占用已启用的 provider 前缀。
    # 读 /api/tags 失败不阻断启动（探针已确认 Ollama 可用），只跳过该校验。
    if any(cfg.enabled for cfg in settings.upstreams):
        try:
            tags = await client.tags()
        except Exception:  # noqa: BLE001
            logger.warning("无法读取本地模型清单，跳过启动期模型 id 冲突校验")
        else:
            try:
                check_local_model_conflicts(
                    settings, [str(t.get("name") or t.get("model") or "") for t in tags]
                )
            except ConfigError as exc:
                logger.error("启动失败：%s", exc)
                logbuffer.record("ERROR", "startup", f"启动失败：{exc}")
                raise
    logger.info(
        "准圣节点启动：%s（%s）→ Ollama %s", settings.node_id, settings.node_name, settings.ollama_base
    )
    logbuffer.record("INFO", "startup", f"节点启动：{settings.node_id}（{settings.node_name}）")

    # 用真实 /api/ps 初始化已加载集合，避免把本来就常驻的模型误判为 loading
    try:
        running = await client.ps()
        app.state.loading.set_loaded(
            [str(m.get("name") or m.get("model") or "") for m in running]
        )
    except Exception:  # noqa: BLE001 - 初始化快照失败不影响启动
        pass

    await _warmup(client, settings, app.state.loading, app.state.warmup_failed)

    try:
        yield
    finally:
        # 优雅停机：标记停止接新并等待在途请求 drain 到超时。
        # 真实 SIGTERM 下 uvicorn 已先关闭 listener，新请求表现为连接拒绝；
        # 这里的标记只覆盖应用内部主动停机的路径（见中间件注释）。
        app.state.shutting_down = True
        drained = await app.state.gate.wait_idle(settings.shutdown_timeout)
        if drained:
            logger.info("在途请求已清空，节点退出")
            logbuffer.record("INFO", "shutdown", "在途请求已清空，节点退出")
        else:
            logger.warning("等待在途请求超时（%.0fs），强制退出", settings.shutdown_timeout)
            logbuffer.record(
                "WARNING", "shutdown", f"等待在途请求超时（{settings.shutdown_timeout:.0f}s），强制退出"
            )
        await client.aclose()
        for runtime in app.state.upstreams:
            await runtime.aclose()


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
        # 优雅停机窗口：应用内部主动置位 shutting_down 时返回 503。
        # 真实 SIGTERM 路径下 uvicorn 会先关闭 listener，新请求表现为连接被拒绝，
        # 因此实测观察不到该分支；保留它用于进程内主动停机与单测覆盖。
        if getattr(request.app.state, "shutting_down", False):
            response = openai_error(
                "节点正在停机，暂不接受新请求",
                status=503,
                type_="server_error",
                code="node_shutting_down",
            )
            response.headers.update(_node_headers(request))
            return response
        response = await call_next(request)
        response.headers.update(_node_headers(request))
        return response

    @app.exception_handler(NodeError)
    async def handle_node_error(request: Request, exc: NodeError):
        return openai_error(exc.message, status=exc.status, type_=exc.type_, code=exc.code)

    @app.exception_handler(ConcurrencyLimitExceeded)
    async def handle_concurrency(request: Request, exc: ConcurrencyLimitExceeded):
        settings: Settings | None = getattr(request.app.state, "settings", None)
        limit = settings.max_concurrency if settings else None
        logbuffer.record("WARNING", "concurrency", f"并发拒绝：已达上限 {limit}，返回 429")
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
        logbuffer.record("ERROR", "exception", f"未捕获异常：{type(exc).__name__}: {exc}")
        return JSONResponse(
            status_code=500,
            content=openai_error_payload("节点内部错误", type_="server_error", code="internal_error"),
            headers=_node_headers(request),
        )

    @app.get("/v1/health")
    async def health(request: Request):
        settings: Settings = request.app.state.settings
        runtimes = _upstream_runtimes(request)
        upstream_states = None
        remote_models: list[dict[str, Any]] = []
        if settings.upstreams:
            loopback = _source_is_loopback(request)
            upstream_states = []
            for runtime in runtimes:
                status = await runtime.refresh_status()
                upstream_states.append(
                    {
                        "id": runtime.cfg.id,
                        "status": status,
                        "active": runtime.active,
                        "max_concurrency": runtime.max_concurrency,
                        "visibility": runtime.cfg.visibility,
                        "data_leaves_lan": runtime.cfg.data_leaves_lan,
                        "last_error": runtime.last_error,
                    }
                )
                if loopback or runtime.cfg.cluster_visible():
                    remote_models.extend(_remote_health_models(runtime))
        return await build_health(
            settings,
            request.app.state.client,
            request.app.state.gate,
            loading=getattr(request.app.state, "loading", None),
            warmup_failed=getattr(request.app.state, "warmup_failed", None),
            upstream_states=upstream_states,
            upstream_models=remote_models,
        )

    @app.get("/v1/models")
    async def models(request: Request):
        settings: Settings = request.app.state.settings
        resp = await request.app.state.client.get("/v1/models")
        # 设计稿 4.2：visibility=local 的远程模型不进入 /v1/models（避免集群发现
        # 一个调用方无权使用的模型）；本地 CLI 仍可按完整 id 调用。
        remote: list[dict[str, Any]] = []
        for runtime in _upstream_runtimes(request):
            if not runtime.cfg.cluster_visible():
                continue
            await runtime.refresh_status()
            remote.extend(_remote_list_entries(runtime))
        return _models_response(resp, remote)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, payload: dict[str, Any]):
        settings: Settings = request.app.state.settings
        client: OllamaClient = request.app.state.client
        gate: ConcurrencyGate = request.app.state.gate
        loading: LoadingTracker | None = getattr(request.app.state, "loading", None)

        model = str(payload.get("model") or "")

        provider_cfg, remote_model = _resolve_remote(settings, model)
        if provider_cfg is not None:
            # 未知模型 / 未授权来源统一 404，不做模糊匹配、不泄漏存在性
            if remote_model is None or not _remote_visible(request, provider_cfg):
                raise _model_not_found(model)
            if "chat" not in remote_model.capabilities:
                raise NodeError(
                    f"模型 {model} 未声明 chat 能力",
                    status=501,
                    code="capability_not_supported",
                    type_="invalid_request_error",
                )
            runtime = _runtime_for(request, provider_cfg)
            if runtime is None:
                raise _model_not_found(model)
            return await _handle_upstream_chat(request, payload, runtime, gate, remote_model)

        _require_capability(settings, model, "chat")

        if bool(payload.get("stream")):
            # 先抢占名额（满了立刻 429），名额**由流结束时**释放——
            # 不能套 async with，否则返回 StreamingResponse 就释放了。
            await gate.acquire()
            if loading is not None:
                loading.note_request(model)
            stack = AsyncExitStack()
            try:
                upstream = await stack.enter_async_context(
                    client.stream_post("/v1/chat/completions", payload)
                )
            except BaseException:
                if loading is not None:
                    loading.mark_failed(model)
                await stack.aclose()
                await gate.release()
                raise

            # 必须先拿到上游响应头再决定回什么：上游 4xx/5xx 时不能包装成
            # 200 + SSE 正文（那样调用方会以为流已开始，错误体被当成 chunk）。
            if upstream.status_code >= 400:
                body = await _read_upstream_error(upstream)
                _record_upstream_error(upstream.status_code, body)
                if loading is not None:
                    loading.mark_failed(model)
                await stack.aclose()
                await gate.release()
                return JSONResponse(status_code=upstream.status_code, content=body)

            return StreamingResponse(
                _stream_body(request, upstream, stack, gate, loading=loading, model=model),
                media_type="text/event-stream",
            )

        async with gate.slot():
            if loading is not None:
                loading.note_request(model)
            resp = await client.post("/v1/chat/completions", payload)
            if loading is not None:
                if resp.status_code < 400:
                    loading.mark_loaded(model)
                else:
                    loading.mark_failed(model)
            return _passthrough(resp)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request, payload: dict[str, Any]):
        settings: Settings = request.app.state.settings
        client: OllamaClient = request.app.state.client
        gate: ConcurrencyGate = request.app.state.gate
        loading: LoadingTracker | None = getattr(request.app.state, "loading", None)

        model = str(payload.get("model") or "")

        # P0 明确不桥接上游 embeddings；远程模型一律按不可用处理
        if _resolve_remote(settings, model)[0] is not None:
            raise _model_not_found(model)

        _require_capability(settings, model, "embedding")

        async with gate.slot():
            if loading is not None:
                loading.note_request(model)
            resp = await client.post("/v1/embeddings", payload)
            if loading is not None:
                if resp.status_code < 400:
                    loading.mark_loaded(model)
                else:
                    loading.mark_failed(model)
            return _passthrough(resp)

    @app.get("/v1/logs")
    async def logs(limit: int = 50):
        """最近的运行事件（白名单环形缓冲，已脱敏；limit 夹到 1..200）。"""
        return JSONResponse(content=logbuffer.recent(limit))

    # 可视控制面板：纯静态页面，本身不需要 token；它调用的 /v1/* 仍走同一套
    # bearer 鉴权（Starlette 的 Mount 不继承 app 级 dependencies，故这里没有
    # 为面板开任何鉴权例外，也没有把 token 写进 HTML）。
    app.mount("/ui", StaticFiles(directory=_UI_DIR, html=True), name="ui")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[ERR] 配置错误，拒绝启动：{exc}", file=sys.stderr)
        raise SystemExit(1)
    uvicorn.run(
        "nodes.quasheng.app:app",
        host=settings.host,
        port=settings.port,
        timeout_graceful_shutdown=int(settings.shutdown_timeout),
    )
