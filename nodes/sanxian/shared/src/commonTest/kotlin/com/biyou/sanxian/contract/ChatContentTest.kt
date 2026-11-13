package com.biyou.sanxian.contract

import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/** 契约 content 双形态（字符串 / 内容数组）的前后兼容回归。 */
class ChatContentTest {

    private val json = Json { encodeDefaults = true }

    @Test
    fun `纯文本 content 解析与序列化保持字符串形态`() {
        val raw = """{"model":"m","messages":[{"role":"user","content":"你好"}]}"""
        val req = json.decodeFromString(ChatRequest.serializer(), raw)
        assertEquals("你好", req.messages.single().content.plainText)

        val encoded = json.encodeToString(ChatRequest.serializer(), req)
        assertTrue(encoded.contains("\"content\":\"你好\""), "文本请求必须仍序列化为字符串，实际=$encoded")
    }

    @Test
    fun `内容数组解析文本与 image_url 并保留顺序`() {
        val raw = """
            {"model":"vl","messages":[{"role":"user","content":[
              {"type":"text","text":"描述"},
              {"type":"image_url","image_url":{"url":"data:image/png;base64,AAAA"}},
              {"type":"text","text":"图中内容"}
            ]}]}
        """.trimIndent()
        val content = json.decodeFromString(ChatRequest.serializer(), raw).messages.single().content
        assertTrue(content is ChatContent.Parts)
        assertEquals("描述图中内容", content.plainText)
        assertEquals(1, content.imageUrls.size)
        assertEquals("data:image/png;base64,AAAA", content.imageUrls.single().url)
    }

    @Test
    fun `assistant 响应始终序列化为字符串`() {
        val message = ChatMessage(role = "assistant", content = ChatContent.Text("答案"))
        val encoded = json.encodeToString(ChatMessage.serializer(), message)
        assertEquals("""{"role":"assistant","content":"答案"}""", encoded)
    }

    @Test
    fun `内容数组缺少 image_url 时按纯文本处理`() {
        val raw = """{"model":"m","messages":[{"role":"user","content":[{"type":"text","text":"只有文本"}]}]}"""
        val content = json.decodeFromString(ChatRequest.serializer(), raw).messages.single().content
        assertEquals("只有文本", content.plainText)
        assertTrue((content as ChatContent.Parts).imageUrls.isEmpty())
    }
}
