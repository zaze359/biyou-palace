package com.biyou.sanxian.ui

import com.biyou.sanxian.llm.ChatMessage
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

/**
 * RequestEnvelope 构造：字段、能力、deadline 与 local_only 拦截。
 */
class ClusterRequestEnvelopeBuilderTest {

    @Test
    fun `构造 cluster 请求并保留 requested_mode`() {
        val envelope = ClusterRequestEnvelopeBuilder.build(
            originNodeId = "sanxian-01",
            requestedMode = ChatMode.AUTO,
            messages = listOf(ChatMessage.user("你好"), ChatMessage.assistant("在")),
        )

        assertEquals("1.0", envelope.schemaVersion)
        assertEquals("cluster", envelope.mode)
        assertEquals("auto", envelope.requestedMode)
        assertEquals("normal", envelope.privacy)
        assertEquals("sanxian-01", envelope.originNodeId)
        assertTrue(envelope.requestId.startsWith("req-"))
        assertEquals(listOf("chat"), envelope.requirements.capabilities)
        assertEquals(listOf("user", "assistant"), envelope.input.messages.map { it.role })
    }

    @Test
    fun `历史有界且空消息被丢弃`() {
        val messages = (1..30).map { ChatMessage.user("m$it") } + ChatMessage.assistant("   ")
        val envelope = ClusterRequestEnvelopeBuilder.build(
            originNodeId = "sanxian-01",
            requestedMode = ChatMode.CLUSTER,
            messages = messages,
        )

        assertEquals(ClusterRequestEnvelopeBuilder.MAX_HISTORY, envelope.input.messages.size)
        assertEquals("m11", envelope.input.messages.first().content.plainText)
        assertEquals("m30", envelope.input.messages.last().content.plainText)
    }

    @Test
    fun `deadline 夹在契约范围内`() {
        fun deadline(value: Int) = ClusterRequestEnvelopeBuilder.build(
            originNodeId = "sanxian-01",
            requestedMode = ChatMode.CLUSTER,
            messages = listOf(ChatMessage.user("hi")),
            deadlineSeconds = value,
        ).deadlineS

        assertEquals(5, deadline(-1))
        assertEquals(600, deadline(9999))
        assertEquals(120, deadline(120))
    }

    @Test
    fun `capabilities 只保留 canonical 值`() {
        val envelope = ClusterRequestEnvelopeBuilder.build(
            originNodeId = "sanxian-01",
            requestedMode = ChatMode.CLUSTER,
            messages = listOf(ChatMessage.user("hi")),
            capabilities = listOf("CHAT", "vision", "unknown", "chat"),
        )

        assertEquals(listOf("chat", "vision"), envelope.requirements.capabilities)
    }

    @Test
    fun `local_only 不得提交阵图`() {
        assertFailsWith<IllegalArgumentException> {
            ClusterRequestEnvelopeBuilder.build(
                originNodeId = "sanxian-01",
                requestedMode = ChatMode.CLUSTER,
                messages = listOf(ChatMessage.user("hi")),
                privacy = "local_only",
            )
        }
    }

    @Test
    fun `本地模式不允许构造阵图请求`() {
        assertFailsWith<IllegalArgumentException> {
            ClusterRequestEnvelopeBuilder.build(
                originNodeId = "sanxian-01",
                requestedMode = ChatMode.LOCAL,
                messages = listOf(ChatMessage.user("hi")),
            )
        }
    }
}
