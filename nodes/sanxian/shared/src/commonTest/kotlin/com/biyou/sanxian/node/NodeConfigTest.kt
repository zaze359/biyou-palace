package com.biyou.sanxian.node

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * NodeConfig 回归用例：
 *  - snake_case 反序列化（shared_token / auth_enabled / preload_models / max_concurrency ...）
 *  - 配置合并优先级：DataStore 用户配置 > assets 默认值
 */
class NodeConfigTest {

    private val assetsJson = """
        {
          "node_id": "sanxian-01",
          "node_name": "散仙·01",
          "port": 4011,
          "shared_token": "assets-token",
          "auth_enabled": true,
          "preload_models": ["Qwen3-0.6B-Q8_0.gguf"],
          "embedding_model": "bge-small-zh.gguf",
          "max_concurrency": 2,
          "mem_warn_pct": 80.0,
          "cpu_warn_pct": 88.0,
          "temp_warn_c": 42.0,
          "request_timeout_ms": 120000,
          "capabilities_map": {
            "Qwen3-0.6B-Q8_0.gguf": { "capabilities": ["chat"], "context_len": 4096 },
            "bge-small-zh.gguf": { "capabilities": ["embedding"], "context_len": 512 }
          }
        }
    """.trimIndent()

    @Test
    fun `snake_case 字段全部映射到 Kotlin 属性`() {
        val config = loadNodeConfig(assetsJson)

        assertEquals("sanxian-01", config.nodeId)
        assertEquals("散仙·01", config.nodeName)
        assertEquals(4011, config.port)
        assertEquals("assets-token", config.sharedToken)
        assertTrue(config.authEnabled)
        assertEquals(listOf("Qwen3-0.6B-Q8_0.gguf"), config.preloadModels)
        assertEquals("bge-small-zh.gguf", config.embeddingModel)
        assertEquals(2, config.maxConcurrency)
        assertEquals(80.0, config.memWarnPct)
        assertEquals(88.0, config.cpuWarnPct)
        assertEquals(42.0, config.tempWarnC)
        assertEquals(120000L, config.requestTimeoutMs)
        assertEquals(4096, config.contextLenFor("Qwen3-0.6B-Q8_0.gguf"))
    }

    @Test
    fun `未知字段忽略、坏 JSON 回退默认值`() {
        val withUnknown = loadNodeConfig("""{"port": 5000, "unknown_key": true}""")
        assertEquals(5000, withUnknown.port)
        assertEquals("sanxian-01", withUnknown.nodeId)

        val broken = loadNodeConfig("{ this is not json")
        assertEquals(NodeConfig(), broken)
        assertNull(loadNodeConfig(null).sharedToken)
    }

    @Test
    fun `fromMap 识别 snake_case 扁平键`() {
        val config = NodeConfig.fromMap(
            mapOf(
                "port" to 4021,
                "shared_token" to "map-token",
                "auth_enabled" to true,
                "max_concurrency" to 3,
                "embedding_model" to "bge.gguf",
                "request_timeout_ms" to 90000,
            ),
        )
        assertEquals(4021, config.port)
        assertEquals("map-token", config.sharedToken)
        assertTrue(config.authEnabled)
        assertEquals(3, config.maxConcurrency)
        assertEquals("bge.gguf", config.embeddingModel)
        assertEquals(90000L, config.requestTimeoutMs)
    }

    // ----------------------------- 合并优先级 -----------------------------

    @Test
    fun `用户配置覆盖 assets 的同名字段`() {
        val assets = loadNodeConfig(assetsJson)
        val merged = assets.withUserOverrides(
            UserConfigOverrides(
                port = 4031,
                sharedToken = "user-token",
                authEnabled = false,
                maxConcurrency = 1,
                chatModel = "Llama-3.2-1B.gguf",
                embeddingModel = "bge-m3.gguf",
            ),
        )

        assertEquals(4031, merged.port)
        assertEquals("user-token", merged.sharedToken)
        assertFalse(merged.authEnabled)
        assertEquals(1, merged.maxConcurrency)
        assertEquals(listOf("Llama-3.2-1B.gguf"), merged.preloadModels)
        assertEquals("bge-m3.gguf", merged.embeddingModel)
    }

    @Test
    fun `未保存的键沿用 assets 默认值`() {
        val assets = loadNodeConfig(assetsJson)
        val merged = assets.withUserOverrides(UserConfigOverrides(port = 5000))

        assertEquals(5000, merged.port)
        assertEquals("assets-token", merged.sharedToken)
        assertTrue(merged.authEnabled)
        assertEquals(assets.preloadModels, merged.preloadModels)
        assertEquals(assets.embeddingModel, merged.embeddingModel)
        assertEquals(assets.maxConcurrency, merged.maxConcurrency)

        assertEquals(assets, assets.withUserOverrides(null))
    }

    @Test
    fun `空字符串表示显式关闭模型`() {
        val assets = loadNodeConfig(assetsJson)
        val merged = assets.withUserOverrides(
            UserConfigOverrides(chatModel = "", embeddingModel = ""),
        )

        assertTrue(merged.preloadModels.isEmpty())
        assertNull(merged.embeddingModel)
    }

    @Test
    fun `空白 token 覆盖不会清空 assets token`() {
        val assets = loadNodeConfig(assetsJson)
        val merged = assets.withUserOverrides(UserConfigOverrides(sharedToken = "   "))
        assertEquals("assets-token", merged.sharedToken)
    }

    @Test
    fun `用户选择新模型时自动补齐能力声明`() {
        val assets = loadNodeConfig(assetsJson).copy(capabilitiesMap = emptyMap())
        val merged = assets.withUserOverrides(
            UserConfigOverrides(
                chatModel = "chat-new.gguf",
                embeddingModel = "embed-new.gguf",
            ),
        )

        assertEquals(listOf("chat"), merged.capabilityFor("chat-new.gguf"))
        assertTrue(merged.supportsCapability("embed-new.gguf", "embedding"))
        assertFalse(merged.supportsCapability("chat-new.gguf", "embedding"))
    }

    @Test
    fun `已登记模型的能力不会被默认 chat 覆盖`() {
        val assets = loadNodeConfig(assetsJson)
        val merged = assets.withUserOverrides(
            UserConfigOverrides(chatModel = "Qwen3-0.6B-Q8_0.gguf"),
        )
        assertEquals(listOf("chat"), merged.capabilityFor("Qwen3-0.6B-Q8_0.gguf"))
        assertEquals(4096, merged.contextLenFor("Qwen3-0.6B-Q8_0.gguf"))
    }
}
