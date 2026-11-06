"""OpenAI 风格错误与节点自定义异常。

契约要求：错误统一为 OpenAI 风格对象，见 docs/node-agent-contract.md。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi.responses import JSONResponse


def openai_error_payload(
    message: str,
    *,
    type_: str = "invalid_request_error",
    code: str | None = None,
) -> dict[str, Any]:
    """构造 OpenAI 风格错误体（不带响应头，便于各 handler 自行附加）。"""
    return {"error": {"message": message, "type": type_, "param": None, "code": code}}


def openai_error(
    message: str,
    *,
    status: int = 400,
    type_: str = "invalid_request_error",
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status, content=openai_error_payload(message, type_=type_, code=code), headers=headers
    )


def normalize_upstream_error(status: int, raw: bytes) -> dict[str, Any]:
    """把上游错误体归一成 OpenAI 风格。

    上游（Ollama）可能返回非 JSON 文本；此时用原始片段作为 message。
    已带 message/type/code 的错误体保留其语义字段，但统一补齐 `param`。
    """
    message = raw[:500].decode("utf-8", "replace").strip()
    upstream_type = None
    upstream_code = None
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and inner.get("message"):
            message = str(inner["message"])
            upstream_type = inner.get("type")
            upstream_code = inner.get("code")
        elif isinstance(inner, str) and inner:
            message = inner
        elif body.get("message"):
            message = str(body["message"])
    if not message:
        message = f"上游返回 HTTP {status}"
    type_ = upstream_type or ("invalid_request_error" if status < 500 else "server_error")
    return openai_error_payload(
        message, type_=str(type_), code=str(upstream_code) if upstream_code else "upstream_error"
    )


class NodeError(Exception):
    """节点侧统一异常；由 app 的 exception handler 转成 OpenAI 错误响应。"""

    def __init__(
        self,
        message: str,
        *,
        status: int = 500,
        code: str = "internal_error",
        type_: str = "server_error",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.type_ = type_


class UpstreamUnavailable(NodeError):
    def __init__(self, message: str = "Ollama 不可达") -> None:
        super().__init__(
            message,
            status=503,
            code="upstream_unavailable",
            type_="server_error",
        )


class ConcurrencyLimitExceeded(NodeError):
    """并发已达上限：立即拒绝，让上层改路由到其他仙徒。"""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"节点并发已达上限 {limit}，请稍后重试或改由其他仙徒承接",
            status=429,
            code="concurrency_limit_exceeded",
            type_="rate_limit_error",
        )
        self.limit = limit
