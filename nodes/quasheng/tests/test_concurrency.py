"""并发闸门单测。

重点锁定一个易错点：**流式请求的名额必须在流结束时才释放**。
若用 `async with gate.slot()` 包裹 `return StreamingResponse(...)`，
handler 一返回就释放名额，而 SSE 是之后才消费的——并发上限会形同虚设。
因此流式路径走 acquire()/release()。
"""
from __future__ import annotations

import asyncio

import pytest

from nodes.quasheng.concurrency import ConcurrencyGate
from nodes.quasheng.errors import ConcurrencyLimitExceeded


def _run(coro):
    return asyncio.run(coro)


def test_acquire_then_release_tracks_active():
    async def scenario():
        gate = ConcurrencyGate(limit=2)
        await gate.acquire()
        assert gate.active == 1
        await gate.acquire()
        assert gate.active == 2
        await gate.release()
        assert gate.active == 1

    _run(scenario())


def test_acquire_raises_when_full():
    async def scenario():
        gate = ConcurrencyGate(limit=1)
        await gate.acquire()
        with pytest.raises(ConcurrencyLimitExceeded):
            await gate.acquire()

    _run(scenario())


def test_slot_releases_on_exit_even_if_inner_raises():
    async def scenario():
        gate = ConcurrencyGate(limit=1)
        with pytest.raises(RuntimeError):
            async with gate.slot():
                assert gate.active == 1
                raise RuntimeError("boom")
        assert gate.active == 0

    _run(scenario())


def test_limit_is_at_least_one():
    assert ConcurrencyGate(limit=0).limit == 1
