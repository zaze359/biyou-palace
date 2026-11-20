"""道果聚合：默认拼接，可选 LLM 润色；失败回退拼接。"""
from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Protocol

from .config import Secret
from .orchestration import chat_url


class Aggregator(Protocol):
    async def aggregate(
        self,
        inputs: list[str],
        *,
        instructions: str | None,
        timeout_s: float,
    ) -> str | None: ...


class ConcatAggregator:
    async def aggregate(
        self,
        inputs: list[str],
        *,
        instructions: str | None,
        timeout_s: float,
    ) -> str | None:
        return _concat(inputs)


class LlmAggregator:
    """经 NewAPI 合并子结果；任何失败都回退拼接。"""

    def __init__(
        self,
        *,
        base_url: str,
        token: Secret | None,
        model: str,
        timeout_s: float = 30.0,
        fallback: Aggregator | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._model = model
        self._timeout_s = timeout_s
        self._fallback = fallback or ConcatAggregator()

    async def aggregate(
        self,
        inputs: list[str],
        *,
        instructions: str | None,
        timeout_s: float,
    ) -> str | None:
        if not inputs:
            return None
        if not self._base_url or not self._token or not self._model:
            return await self._fallback.aggregate(
                inputs, instructions=instructions, timeout_s=timeout_s
            )
        try:
            answer = await asyncio.wait_for(
                asyncio.to_thread(
                    self._request, inputs, instructions, timeout_s or self._timeout_s
                ),
                timeout=timeout_s or self._timeout_s,
            )
        except Exception:
            return await self._fallback.aggregate(
                inputs, instructions=instructions, timeout_s=timeout_s
            )
        return answer or await self._fallback.aggregate(
            inputs, instructions=instructions, timeout_s=timeout_s
        )

    def _request(
        self, inputs: list[str], instructions: str | None, timeout_s: float
    ) -> str:
        system = (
            "你是碧游宫的道果聚合器。把多路子结果合并成一段最终回答。"
            "保留关键事实与差异，不编造，不提及内部步骤 id。"
        )
        joined = "\n\n".join(f"[{index + 1}] {text}" for index, text in enumerate(inputs))
        user = (instructions.strip() + "\n\n" if instructions else "") + joined
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "temperature": 0.2,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token.get()}",
        }
        req = urllib.request.Request(
            chat_url(self._base_url), data=data, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            body = json.loads(response.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        return str(content or "").strip()


def _concat(inputs: list[str]) -> str | None:
    ordered = [text for text in inputs if text]
    return "\n\n".join(ordered) if ordered else None
