package com.biyou.sanxian.node

import com.biyou.sanxian.contract.usageOf
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * 能力判定与 usage 口径。
 *
 * 端点层的 501 映射在 androidMain（NodeServer），本轮用共享层的同一判定函数
 * [NodeConfig.supportsCapability] + [capabilityNotSupported] 锁定行为；
 * 真正的 HTTP 端点回归放到第二步集成测试。
 */
class CapabilityUsageTest {

    private val config = NodeConfig(
        capabilitiesMap = mapOf(
            "chat-only.gguf" to ModelCapability(listOf("chat")),
            "embed.gguf" to ModelCapability(listOf("embedding"), contextLen = 512),
            "both.gguf" to ModelCapability(listOf("chat", "embedding")),
        ),
    )

    @Test
    fun `chat-only 模型不支持 embeddings`() {
        assertFalse(config.supportsCapability("chat-only.gguf", "embedding"))
        assertTrue(config.supportsCapability("chat-only.gguf", "chat"))
    }

    @Test
    fun `未声明能力的模型默认 chat-only`() {
        assertEquals(listOf("chat"), config.capabilityFor("unknown.gguf"))
        assertFalse(config.supportsCapability("unknown.gguf", "embedding"))
    }

    @Test
    fun `embedding 能力按登记生效`() {
        assertTrue(config.supportsCapability("embed.gguf", "embedding"))
        assertTrue(config.supportsCapability("both.gguf", "embedding"))
        assertEquals(512, config.contextLenFor("embed.gguf"))
    }

    @Test
    fun `能力不支持映射为 501 与 OpenAI 错误体`() {
        val error = capabilityNotSupported("模型 chat-only.gguf 未声明 embedding 能力")
        assertEquals(501, error.status)
        assertEquals("capability_not_supported", error.code)

        val (status, body) = error.toOpenAIError()
        assertEquals(501, status)
        val json = Json.parseToJsonElement(body).jsonObject["error"]!!.jsonObject
        assertEquals("模型 chat-only.gguf 未声明 embedding 能力", json["message"]!!.jsonPrimitive.content)
        assertEquals("capability_not_supported", json["code"]!!.jsonPrimitive.content)
        assertEquals("invalid_request_error", json["type"]!!.jsonPrimitive.content)
    }

    @Test
    fun `usage 使用裁剪后的真实 prompt token 数`() {
        // 原始历史假设 1000 tokens，原生层裁剪后实际送入 42 tokens：
        // 调用方必须传 stats.promptTokens（42），usage 不得回退成原始长度。
        val usage = usageOf(promptTokens = 42, completionTokens = 7)
        assertEquals(42, usage.promptTokens)
        assertEquals(7, usage.completionTokens)
        assertEquals(49, usage.totalTokens)
    }

    @Test
    fun `embeddings usage 不含生成 token`() {
        val usage = usageOf(promptTokens = 128, completionTokens = 0)
        assertEquals(128, usage.totalTokens)
        assertEquals(0, usage.completionTokens)
    }
}
