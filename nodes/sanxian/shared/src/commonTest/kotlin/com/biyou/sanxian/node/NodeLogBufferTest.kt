package com.biyou.sanxian.node

import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/** 环形缓冲的纯逻辑回归：夹取、容量淘汰、脱敏、白名单。 */
class NodeLogBufferTest {

    @BeforeTest
    fun reset() = NodeLogBuffer.clear()

    @Test
    fun `limit 夹到 1 到 200 且返回最近若干条`() {
        repeat(5) { NodeLogBuffer.record("INFO", "startup", "e$it") }

        val two = NodeLogBuffer.recent(2)
        assertEquals(2, two.limit)
        assertEquals(listOf("e3", "e4"), two.lines.map { it.message })

        assertEquals(1, NodeLogBuffer.recent(0).limit)
        assertEquals(200, NodeLogBuffer.recent(9999).limit)
        assertEquals(5, NodeLogBuffer.recent(50).total)
        assertEquals(200, NodeLogBuffer.recent(50).capacity)
    }

    @Test
    fun `容量 200 时淘汰最旧事件但 total 持续累加`() {
        repeat(205) { NodeLogBuffer.record("INFO", "startup", "e$it") }

        val body = NodeLogBuffer.recent(200)
        assertEquals(200, body.lines.size)
        assertEquals("e5", body.lines.first().message)
        assertEquals("e204", body.lines.last().message)
        assertEquals(205, body.total)
    }

    @Test
    fun `写入前脱敏 token 与绝对路径并截断 500 字`() {
        NodeLogBuffer.configure("secret-token")
        NodeLogBuffer.record(
            "ERROR",
            "exception",
            "token=secret-token at /data/user/0/app/files/a.gguf " + "x".repeat(600),
        )

        val line = NodeLogBuffer.recent(1).lines.single()
        assertFalse(line.message.contains("secret-token"))
        assertFalse(line.message.contains("/data/user"))
        assertTrue(line.message.contains("***"))
        assertTrue(line.message.contains("<path>"))
        assertEquals(500, line.message.length)
    }

    @Test
    fun `非白名单事件被丢弃且非法级别归为 INFO`() {
        NodeLogBuffer.record("INFO", "debug", "不该被记录")
        assertEquals(0, NodeLogBuffer.recent(10).total)

        NodeLogBuffer.record("DEBUG", "startup", "级别非法")
        val line = NodeLogBuffer.recent(1).lines.single()
        assertEquals("INFO", line.level)
        assertEquals("startup", line.event)
    }
}
