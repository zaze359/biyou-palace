"""并发闸门。

Ollama 自身没有任何限流，因此并发管控必须由节点实现（契约的
limits.max_concurrency + resources.queue_len）。

已定策略：**满载立即拒绝 429 + Retry-After**，不做本地排队——
集群里有别的仙徒可能正闲着，本地排队不如让上层立刻换节点。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from .errors import ConcurrencyLimitExceeded


class ConcurrencyGate:
    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._active = 0
        self._lock = asyncio.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        """在途请求数，供 /v1/health 的 resources.queue_len 使用。"""
        return self._active

    async def acquire(self) -> None:
        """抢占一个名额，已满则抛 429。

        流式响应不能直接用 slot()：handler 返回 StreamingResponse 时
        slot 就退出了，而 SSE 是之后才被消费的——那样流式请求全程不占
        名额，并发上限形同虚设。流式场景必须 acquire() 后由流结束再
        release()。
        """
        async with self._lock:
            if self._active >= self._limit:
                raise ConcurrencyLimitExceeded(self._limit)
            self._active += 1

    async def release(self) -> None:
        async with self._lock:
            self._active = max(0, self._active - 1)

    @asynccontextmanager
    async def slot(self):
        """非流式请求的常规用法：进出自动增减。"""
        await self.acquire()
        try:
            yield
        finally:
            await self.release()
