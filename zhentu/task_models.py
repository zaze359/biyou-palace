"""任务提交请求模型（编排协议 v1.0 的 Python 侧校验）。"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import normalize_capability

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class ChatMessageIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(min_length=0, max_length=200_000)

    @field_validator("content")
    @classmethod
    def _check_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("messages[].content 不能为空")
        return value


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    messages: list[ChatMessageIn] = Field(min_length=1, max_length=200)
    attachments: list[Any] = Field(default_factory=list, max_length=20)


class TaskRequirements(BaseModel):
    model_config = ConfigDict(extra="ignore")

    capabilities: list[str] = Field(default_factory=lambda: ["chat"], min_length=1)
    context_len: int | None = None

    @field_validator("capabilities")
    @classmethod
    def _normalize_caps(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for item in values:
            cap = normalize_capability(item)
            if cap not in out:
                out.append(cap)
        return out

    @field_validator("context_len")
    @classmethod
    def _check_context(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("requirements.context_len 必须大于 0")
        return value


class TaskCreateRequest(BaseModel):
    """`POST /zhentu/v1/requests` 请求体。"""

    model_config = ConfigDict(extra="ignore")

    schema_version: Literal["1.0"] = "1.0"
    request_id: str
    session_id: str | None = None
    origin_node_id: str
    mode: Literal["cluster"] = "cluster"
    requested_mode: Literal["local", "cluster", "auto"] | None = None
    privacy: Literal["normal", "local_only"] = "normal"
    deadline_s: int = Field(default=120, ge=5, le=600)
    input: TaskInput
    requirements: TaskRequirements = Field(default_factory=TaskRequirements)

    @field_validator("request_id", "origin_node_id", "session_id")
    @classmethod
    def _check_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _ID_RE.fullmatch(value):
            raise ValueError("id 必须是 1..128 位字母/数字/._:-")
        return value
