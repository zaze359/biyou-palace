"""Mock Ollama：在不安装 Ollama 的机器上验证准圣节点的完整链路。

覆盖节点需要的全部上游端点：
- /v1/models（启动探活，决定能否启动）
- /api/tags、/api/ps、/api/version（/v1/health 的数据来源）
- /v1/chat/completions（非流式 + SSE 流式，含 data: [DONE]）
- /v1/embeddings
- /api/generate、/api/chat（预热）

刻意放慢流式输出（ chunks 之间 sleep），以便验证并发闸门的 429。
启动：uvicorn nodes.quasheng.tests.mock_ollama:app --port 11434
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mock-ollama")

app = FastAPI(title="Mock Ollama")

MODELS = ["mock-7b", "mock-embed"]
# 节点配置里声明 mock-7b 只有 chat、mock-embed 只有 embedding，
# 用于验证「未声明能力 → 501」的能力驱动路由。


@app.middleware("http")
async def log_upstream_headers(request: Request, call_next):
    """记录上游收到的请求头。

    用于验证：节点**不会**把调用方的 Authorization 转发给 Ollama。
    """
    auth = request.headers.get("authorization")
    logger.info(
        "[mock] %s %s authorization=%s",
        request.method,
        request.url.path,
        auth if auth else "<none>",
    )
    return await call_next(request)


@app.get("/api/version")
async def version():
    return {"version": "0.5.7"}


@app.get("/api/tags")
async def tags():
    return {
        "models": [
            {
                "name": "mock-7b",
                "model": "mock-7b",
                "details": {"family": "qwen2", "parameter_size": "7B"},
            },
            {
                "name": "mock-embed",
                "model": "mock-embed",
                "details": {"family": "nomic", "parameter_size": "137M"},
            },
        ]
    }


@app.get("/api/ps")
async def ps():
    """mock-7b 已加载（配合预热白名单验证 loaded=true）。"""
    return {"models": [{"name": "mock-7b", "size_vram": 0}]}


@app.get("/v1/models")
async def openai_models():
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": "mock"} for m in MODELS],
    }


@app.post("/api/generate")
async def generate():
    return {"model": "mock-7b", "response": "", "done": True}


@app.post("/api/chat")
async def chat_native():
    return {"model": "mock-7b", "message": {"role": "assistant", "content": "ok"}, "done": True}


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    payload = await request.json()
    texts = payload.get("input") or []
    if isinstance(texts, str):
        texts = [texts]
    return {
        "object": "list",
        "model": payload.get("model"),
        "data": [
            {"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]}
            for i, _ in enumerate(texts)
        ],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }


def _chunk(content: str | None, finish: str | None = None) -> str:
    delta: dict = {}
    if content is not None:
        delta["content"] = content
    return json.dumps(
        {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "model": "mock-7b",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        },
        ensure_ascii=False,
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload = await request.json()
    stream = bool(payload.get("stream"))

    if not stream:
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": payload.get("model"),
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "你好，我是准圣节点。"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
        }

    async def gen():
        # 刻意放慢，便于并发闸门 429 的验证（max_concurrency=2 时第三个应被拒）
        delay = float(os.environ.get("MOCK_STREAM_DELAY", "0.8"))
        yield f"data: {_chunk('', None)}\n\n".replace('"content": ""', '"role": "assistant"')
        await asyncio.sleep(delay)
        yield f"data: {_chunk('你好')}\n\n"
        await asyncio.sleep(delay)
        yield f"data: {_chunk('，我是准圣节点。')}\n\n"
        await asyncio.sleep(delay)
        # 结束标记 chunk：delta 里没有 content（验证客户端 None 保护）
        yield f"data: {_chunk(None, 'stop')}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True})
