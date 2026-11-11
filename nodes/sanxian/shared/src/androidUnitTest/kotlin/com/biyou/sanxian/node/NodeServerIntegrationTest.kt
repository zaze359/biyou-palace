package com.biyou.sanxian.node

import com.biyou.sanxian.contract.Resources
import com.biyou.sanxian.llm.ChatSession
import com.biyou.sanxian.llm.GenerateStats
import com.biyou.sanxian.llm.LlmConfig
import com.biyou.sanxian.llm.LlmEngine
import com.biyou.sanxian.llm.ModelInfo
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.contentType
import io.ktor.server.testing.ApplicationTestBuilder
import io.ktor.server.testing.testApplication
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * 端点级集成测试：在 JVM 上用 Ktor `testApplication` 跑完整路由链路
 * （StatusPages / 鉴权 / 并发闸门 / 响应头 / SSE 拼装），引擎与健康采集用假实现替换。
 *
 * 之所以可行：路由已从 [NodeServer] 抽到 [installNodeApi]，不依赖 Android API，
 * 因此**不需要 Robolectric、真机或真实模型**。
 */
class NodeServerIntegrationTest {

    // ----------------------------- 假实现 -----------------------------

    /** 假引擎：吐固定分块并回填 usage；不加载任何模型。 */
    private class FakeLlmEngine(
        private val chunks: List<String> = listOf("你", "好"),
        private val loaded: Boolean = true,
    ) : LlmEngine {
        override fun loadModel(config: LlmConfig) = Unit
        override fun isModelLoaded(): Boolean = loaded
        override fun generate(
            session: ChatSession,
            prompt: String,
            maxTokens: Int,
            stats: GenerateStats?,
        ): Flow<String> = flow {
            chunks.forEach { emit(it) }
            stats?.apply {
                promptTokens = 12
                completionTokens = chunks.size
            }
        }

        override fun stopGeneration() = Unit
        override fun getModelInfo(): ModelInfo? =
            if (loaded) ModelInfo(name = "Qwen3 0.6B", contextSize = 4096, backend = "llama.cpp") else null

        override fun close() = Unit
    }

    /** 假健康采集：固定返回正常资源，避免触碰 Android API。 */
    private val fakeHealth: suspend (Int) -> NodeHealthCollector.CollectedHealth = { queueLen ->
        NodeHealthCollector.CollectedHealth(
            resources = Resources(
                cpuPct = 10.0,
                memUsedMb = 1024,
                memTotalMb = 4096,
                diskFreeMb = 8192,
                queueLen = queueLen,
                batteryPct = 88.0,
                temperatureC = 30.0,
                thermalState = "nominal",
            ),
            memPct = 25.0,
            cpuPct = 10.0,
            tempC = 30.0,
        )
    }

    private val baseConfig = NodeConfig(
        nodeId = "sanxian-test",
        nodeName = "散仙·测试",
        capabilitiesMap = mapOf("qwen.gguf" to ModelCapability(listOf("chat"), contextLen = 4096)),
        maxConcurrency = 2,
    )

    private fun ApplicationTestBuilder.installTestApi(
        config: NodeConfig = baseConfig,
        engine: LlmEngine = FakeLlmEngine(),
        embedding: EmbeddingEngine? = null,
        gate: ConcurrencyGate = ConcurrencyGate(config.maxConcurrency),
    ) {
        application {
            installNodeApi(config, engine, embedding, gate, fakeHealth)
        }
    }

    private fun assertContractHeaders(response: HttpResponse, nodeId: String = "sanxian-test") {
        assertEquals(nodeId, response.headers[Contract.HEADER_NODE_ID])
        assertEquals(Contract.ROLE, response.headers[Contract.HEADER_NODE_ROLE])
        assertEquals(Contract.VERSION, response.headers[Contract.HEADER_CONTRACT_VERSION])
    }

    private fun JsonObject.errorBody(): JsonObject = this["error"]!!.jsonObject

    private fun JsonObject.int(field: String): Int =
        this[field]!!.jsonPrimitive.content.toInt()

    private val json = Json

    // ----------------------------- /v1/health -----------------------------

    @Test
    fun `health 返回契约结构与三件套响应头`() = testApplication {
        installTestApi()

        val res = client.get("/v1/health")
        assertEquals(HttpStatusCode.OK, res.status)
        assertContractHeaders(res)

        val body = json.parseToJsonElement(res.bodyAsText()).jsonObject
        val node = body["node"]!!.jsonObject
        assertEquals("sanxian-test", node["id"]!!.jsonPrimitive.content)
        assertEquals("sanxian", node["role"]!!.jsonPrimitive.content)
        assertEquals("1.0", node["contract_version"]!!.jsonPrimitive.content)
        assertEquals("llama.cpp", node["runtime"]!!.jsonPrimitive.content)
        assertEquals("ready", body["status"]!!.jsonPrimitive.content)

        val model = body["models"]!!.jsonArray.single().jsonObject
        assertEquals("qwen.gguf", model["name"]!!.jsonPrimitive.content)
        assertEquals(4096, model["context_len"]!!.jsonPrimitive.content.toInt())
        assertTrue(model["capabilities"]!!.jsonArray.map { it.jsonPrimitive.content }.contains("chat"))

        val limits = body["limits"]!!.jsonObject
        assertEquals(2, limits.int("max_concurrency"))
        assertEquals(4096, limits["max_context_len"]!!.jsonPrimitive.content.toInt())

        assertEquals("nominal", body["resources"]!!.jsonObject["thermal_state"]!!.jsonPrimitive.content)
    }

    @Test
    fun `health 的 models 反映引擎真实加载状态`() = testApplication {
        installTestApi(config = baseConfig.copy(preloadModels = listOf("/data/models/qwen.gguf")))

        val body = json.parseToJsonElement(client.get("/v1/health").bodyAsText()).jsonObject
        val model = body["models"]!!.jsonArray.single().jsonObject
        assertEquals("qwen.gguf", model["name"]!!.jsonPrimitive.content)
        assertTrue(model["loaded"]!!.jsonPrimitive.content.toBoolean())
    }

    // ----------------------------- 鉴权 -----------------------------

    @Test
    fun `auth 开启时无 token 返回 401 且错误体与响应头齐全`() = testApplication {
        installTestApi(config = baseConfig.copy(authEnabled = true, sharedToken = "test-token-only"))

        val res = client.get("/v1/health")
        assertEquals(HttpStatusCode.Unauthorized, res.status)
        assertContractHeaders(res)

        val error = json.parseToJsonElement(res.bodyAsText()).jsonObject.errorBody()
        assertEquals("invalid_api_key", error["code"]!!.jsonPrimitive.content)
        assertEquals("invalid_request_error", error["type"]!!.jsonPrimitive.content)
        assertEquals(JsonNull, error["param"])
    }

    @Test
    fun `auth 开启时带正确 token 可访问`() = testApplication {
        installTestApi(config = baseConfig.copy(authEnabled = true, sharedToken = "test-token-only"))

        val res = client.get("/v1/health") {
            header(HttpHeaders.Authorization, "Bearer test-token-only")
        }
        assertEquals(HttpStatusCode.OK, res.status)
    }

    // ----------------------------- chat -----------------------------

    @Test
    fun `chat 非流式返回 OpenAI 结构且 usage 取真实 token 数`() = testApplication {
        installTestApi()

        val res = client.post("/v1/chat/completions") {
            contentType(ContentType.Application.Json)
            setBody("""{"model":"qwen.gguf","messages":[{"role":"user","content":"你好"}],"stream":false}""")
        }
        assertEquals(HttpStatusCode.OK, res.status)
        assertContractHeaders(res)

        val body = json.parseToJsonElement(res.bodyAsText()).jsonObject
        assertEquals("chat.completion", body["object"]!!.jsonPrimitive.content)
        assertEquals("qwen.gguf", body["model"]!!.jsonPrimitive.content)

        val choice = body["choices"]!!.jsonArray.single().jsonObject
        assertEquals("stop", choice["finish_reason"]!!.jsonPrimitive.content)
        val message = choice["message"]!!.jsonObject
        assertEquals("assistant", message["role"]!!.jsonPrimitive.content)
        assertEquals("你好", message["content"]!!.jsonPrimitive.content)

        val usage = body["usage"]!!.jsonObject
        assertEquals(12, usage.int("prompt_tokens"))
        assertEquals(2, usage.int("completion_tokens"))
        assertEquals(14, usage.int("total_tokens"))
    }

    @Test
    fun `chat 流式按 role-content-stop-DONE 顺序输出 SSE 帧`() = testApplication {
        installTestApi()

        val res = client.post("/v1/chat/completions") {
            contentType(ContentType.Application.Json)
            setBody("""{"model":"qwen.gguf","messages":[{"role":"user","content":"你好"}],"stream":true}""")
        }
        assertEquals(HttpStatusCode.OK, res.status)
        assertTrue(
            res.headers[HttpHeaders.ContentType].orEmpty().startsWith("text/event-stream"),
            "Content-Type 应为 text/event-stream，实际=${res.headers[HttpHeaders.ContentType]}",
        )

        val text = res.bodyAsText()
        val role = text.indexOf(""""delta":{"role":"assistant"}""")
        val content1 = text.indexOf(""""delta":{"content":"你"}""")
        val content2 = text.indexOf(""""delta":{"content":"好"}""")
        val stop = text.indexOf(""""delta":{}""")
        val done = text.indexOf("data: [DONE]")

        assertTrue(role >= 0, "缺少首帧 role 块，实际响应：\n$text")
        assertTrue(role < content1, "role 帧必须早于首个 content 帧")
        assertTrue(content1 < content2, "content 帧需按生成顺序输出")
        assertTrue(content2 < stop, "stop 帧必须在所有 content 帧之后")
        assertTrue(stop < done, "[DONE] 必须是最后一帧")
        assertTrue(text.trimEnd().endsWith("data: [DONE]"), "响应必须以 [DONE] 收尾")
        assertTrue(text.contains("\"finish_reason\":\"stop\""))

        // 每个 SSE 事件以空行分隔（帧格式正确性）
        val events = text.split("\n\n").filter { it.isNotBlank() }
        assertEquals(5, events.size, "应为 role + 2×content + stop + DONE 共 5 帧，实际：$events")
        assertTrue(events.all { it.startsWith("data: ") })
    }

    // ----------------------------- embeddings -----------------------------

    @Test
    fun `chat-only 模型请求 embeddings 返回 501 与 OpenAI 错误体`() = testApplication {
        installTestApi()

        val res = client.post("/v1/embeddings") {
            contentType(ContentType.Application.Json)
            setBody("""{"model":"qwen.gguf","input":"hello"}""")
        }
        assertEquals(HttpStatusCode.NotImplemented, res.status)
        assertContractHeaders(res)

        val error = json.parseToJsonElement(res.bodyAsText()).jsonObject.errorBody()
        assertEquals("capability_not_supported", error["code"]!!.jsonPrimitive.content)
        assertEquals("invalid_request_error", error["type"]!!.jsonPrimitive.content)
    }

    // ----------------------------- 通用错误 -----------------------------

    @Test
    fun `未知路由返回 404 且仍带三件套响应头`() = testApplication {
        installTestApi()

        val res = client.get("/v1/unknown")
        assertEquals(HttpStatusCode.NotFound, res.status)
        assertContractHeaders(res)
    }

    @Test
    fun `非法 JSON 返回 400 与 OpenAI 错误体`() = testApplication {
        installTestApi()

        val res = client.post("/v1/chat/completions") {
            contentType(ContentType.Application.Json)
            setBody("""{"model": "qwen.gguf", "messages": [""")
        }
        assertEquals(HttpStatusCode.BadRequest, res.status)
        assertContractHeaders(res)

        val error = json.parseToJsonElement(res.bodyAsText()).jsonObject.errorBody()
        assertEquals("invalid_parameter", error["code"]!!.jsonPrimitive.content)
        assertEquals("invalid_request_error", error["type"]!!.jsonPrimitive.content)
    }

    // ----------------------------- 并发闸门 -----------------------------

    @Test
    fun `并发满载返回 429 与 Retry-After 且 health 上报 queue_len`() = testApplication {
        val gate = ConcurrencyGate(limit = 1)
        installTestApi(config = baseConfig.copy(maxConcurrency = 1), gate = gate)

        gate.acquire()
        try {
            val health = json.parseToJsonElement(client.get("/v1/health").bodyAsText()).jsonObject
            assertEquals(1, health["resources"]!!.jsonObject.int("queue_len"))

            val res = client.post("/v1/chat/completions") {
                contentType(ContentType.Application.Json)
                setBody("""{"model":"qwen.gguf","messages":[{"role":"user","content":"hi"}],"stream":false}""")
            }
            assertEquals(HttpStatusCode.TooManyRequests, res.status)
            assertEquals("1", res.headers["Retry-After"])
            assertContractHeaders(res)

            val error = json.parseToJsonElement(res.bodyAsText()).jsonObject.errorBody()
            assertEquals("rate_limit_exceeded", error["code"]!!.jsonPrimitive.content)
        } finally {
            gate.release()
        }

        // 名额释放后恢复可服务
        val after = client.post("/v1/chat/completions") {
            contentType(ContentType.Application.Json)
            setBody("""{"model":"qwen.gguf","messages":[{"role":"user","content":"hi"}],"stream":false}""")
        }
        assertEquals(HttpStatusCode.OK, after.status)
    }
}
