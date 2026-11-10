package com.biyou.sanxian.node

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

/**
 * 并发闸门：满载立即 429（不排队），名额必须在请求结束时释放。
 *
 * 说明：当前实现是「立即拒绝」而非排队，因此不存在「排队请求取消误杀他人」的路径；
 * 流式请求的取消安全由调用方在 finally 中 release 保证（见 NodeServer）。
 */
class ConcurrencyGateTest {

    @Test
    fun `允许到上限后立即拒绝并返回 429`() {
        val gate = ConcurrencyGate(limit = 2)
        gate.acquire()
        gate.acquire()
        assertEquals(2, gate.active)

        val error = assertFailsWith<NodeException> { gate.acquire() }
        assertEquals(429, error.status)
        assertEquals("rate_limit_exceeded", error.code)
        // 被拒绝的请求不得占用名额
        assertEquals(2, gate.active)
    }

    @Test
    fun `释放后名额可复用且计数归零`() {
        val gate = ConcurrencyGate(limit = 2)
        gate.acquire()
        gate.acquire()
        gate.release()
        assertEquals(1, gate.active)

        gate.acquire()
        assertEquals(2, gate.active)

        gate.release()
        gate.release()
        assertEquals(0, gate.active)
    }

    @Test
    fun `异常路径用 finally 释放不会泄漏名额`() {
        val gate = ConcurrencyGate(limit = 1)
        assertFailsWith<IllegalStateException> {
            gate.acquire()
            try {
                error("模拟推理失败")
            } finally {
                gate.release()
            }
        }
        assertEquals(0, gate.active)
        gate.acquire()
        assertEquals(1, gate.active)
    }

    @Test
    fun `上限必须为正数`() {
        assertFailsWith<IllegalArgumentException> { ConcurrencyGate(0) }
    }
}
