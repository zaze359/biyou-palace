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
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
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
import kotlin.test.assertFalse
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
        /** 聊天链路不得触碰引擎：加载/释放次数用于回归断言。 */
        var loadCalls = 0
        var closeCalls = 0

        override fun loadModel(config: LlmConfig) {
            loadCalls++
        }
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

        override fun close() {
            closeCalls++
        }
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
        health: suspend (Int) -> NodeHealthCollector.CollectedHealth = fakeHealth,
        governor: ThermalGovernor = ThermalGovernor(config),
        thermalProbe: () -> ThermalSignal = { ThermalSignal() },
    ) {
        application {
            installNodeApi(config, engine, embedding, gate, health, governor, thermalProbe)
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

    @Test
    fun `聊天请求不触发引擎 load 与 close，节点服务持续可用`() = testApplication {
        val engine = FakeLlmEngine()
        installTestApi(
            config = baseConfig.copy(preloadModels = listOf("/data/models/qwen.gguf")),
            engine = engine,
        )

        repeat(2) { i ->
            val res = client.post("/v1/chat/completions") {
                contentType(ContentType.Application.Json)
                setBody("""{"model":"qwen.gguf","messages":[{"role":"user","content":"第${i}次"}],"stream":false}""")
            }
            assertEquals(HttpStatusCode.OK, res.status)
        }
        assertEquals(0, engine.loadCalls, "聊天请求不得触发 loadModel")
        assertEquals(0, engine.closeCalls, "聊天请求不得 close 引擎句柄")

        val health = json.parseToJsonElement(client.get("/v1/health").bodyAsText()).jsonObject
        assertEquals("ready", health["status"]!!.jsonPrimitive.content)
        assertTrue(health["models"]!!.jsonArray.single().jsonObject["loaded"]!!.jsonPrimitive.content.toBoolean())
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

    // ----------------------------- /v1/logs -----------------------------

    @Test
    fun `logs 端点受 bearer 鉴权并返回契约结构`() = testApplication {
        NodeLogBuffer.clear()
        installTestApi(config = baseConfig.copy(authEnabled = true, sharedToken = "logs-token"))

        val denied = client.get("/v1/logs")
        assertEquals(HttpStatusCode.Unauthorized, denied.status)
        assertEquals(
            "invalid_api_key",
            json.parseToJsonElement(denied.bodyAsText()).jsonObject.errorBody()["code"]!!.jsonPrimitive.content,
        )

        val allowed = client.get("/v1/logs") {
            header(HttpHeaders.Authorization, "Bearer logs-token")
        }
        assertEquals(HttpStatusCode.OK, allowed.status)
        assertContractHeaders(allowed)

        val body = json.parseToJsonElement(allowed.bodyAsText()).jsonObject
        assertEquals(200, body.int("capacity"))
        assertEquals(50, body.int("limit"))
        assertEquals(1, body.int("total"))
        val line = body["lines"]!!.jsonArray.single().jsonObject
        assertEquals(setOf("time", "level", "event", "message"), line.keys)
        assertEquals("auth", line["event"]!!.jsonPrimitive.content)
        // 401 事件不得泄漏调用方 token
        assertFalse(line["message"]!!.jsonPrimitive.content.contains("logs-token"))
    }

    @Test
    fun `logs limit 夹取且非法参数返回 400`() = testApplication {
        NodeLogBuffer.clear()
        repeat(5) { NodeLogBuffer.record("INFO", "startup", "e$it") }
        installTestApi()

        suspend fun logs(query: String): JsonObject =
            json.parseToJsonElement(client.get("/v1/logs$query").bodyAsText()).jsonObject

        val two = logs("?limit=2")
        assertEquals(2, two.int("limit"))
        assertEquals(
            listOf("e3", "e4"),
            two["lines"]!!.jsonArray.map { it.jsonObject["message"]!!.jsonPrimitive.content },
        )
        assertEquals(1, logs("?limit=0").int("limit"))
        assertEquals(200, logs("?limit=9999").int("limit"))

        val bad = client.get("/v1/logs?limit=abc")
        assertEquals(HttpStatusCode.BadRequest, bad.status)
        assertEquals(
            "invalid_parameter",
            json.parseToJsonElement(bad.bodyAsText()).jsonObject.errorBody()["code"]!!.jsonPrimitive.content,
        )
    }

    @Test
    fun `logs 写入前脱敏 token 与绝对路径`() = testApplication {
        NodeLogBuffer.clear()
        installTestApi(config = baseConfig.copy(sharedToken = "secret-token-value"))
        // 先发一次请求让路由装载（installNodeApi 会登记 token），再写入待脱敏事件
        client.get("/v1/health")
        NodeLogBuffer.record(
            "ERROR",
            "exception",
            "boom at /storage/emulated/0/Android/data/app/models/a.gguf token=secret-token-value",
        )

        val body = json.parseToJsonElement(client.get("/v1/logs?limit=1").bodyAsText()).jsonObject
        val message = body["lines"]!!.jsonArray.single().jsonObject["message"]!!.jsonPrimitive.content
        assertFalse(message.contains("secret-token-value"))
        assertFalse(message.contains("/storage"))
        assertTrue(message.contains("***"))
        assertTrue(message.contains("<path>"))
    }

    // ----------------------------- 发热降载 -----------------------------

    /** 慢引擎：用来验证降载不会中断已在途的流式请求。 */
    private class SlowLlmEngine(private val delayMs: Long = 150) : LlmEngine {
        override fun loadModel(config: LlmConfig) = Unit
        override fun isModelLoaded(): Boolean = true
        override fun generate(
            session: ChatSession,
            prompt: String,
            maxTokens: Int,
            stats: GenerateStats?,
        ): Flow<String> = flow {
            listOf("你", "好", "呀").forEach {
                delay(delayMs)
                emit(it)
            }
            stats?.apply {
                promptTokens = 12
                completionTokens = 3
            }
        }

        override fun stopGeneration() = Unit
        override fun getModelInfo(): ModelInfo = ModelInfo("Qwen3 0.6B", 4096, "llama.cpp")
        override fun close() = Unit
    }

    /** 健康采集跟随注入的热信号，保证 /v1/health 与准入判断同源。 */
    private fun thermalHealth(
        signal: () -> ThermalSignal,
    ): suspend (Int) -> NodeHealthCollector.CollectedHealth = { queueLen ->
        val s = signal()
        NodeHealthCollector.CollectedHealth(
            resources = Resources(
                cpuPct = 10.0,
                memUsedMb = 1024,
                memTotalMb = 4096,
                diskFreeMb = 8192,
                queueLen = queueLen,
                batteryPct = 88.0,
                temperatureC = s.tempC,
                thermalState = if ((s.statusLevel ?: 0) >= 3) "warn" else "nominal",
            ),
            memPct = 25.0,
            cpuPct = 10.0,
            tempC = s.tempC,
            thermalStatusLevel = s.statusLevel,
        )
    }

    @Test
    fun `过热时新请求 503 且健康暴露降载，恢复后自动放行`() = testApplication {
        var signal = ThermalSignal(statusLevel = 3, tempC = 50.0)
        installTestApi(health = thermalHealth { signal }, thermalProbe = { signal })

        suspend fun chat(): HttpResponse = client.post("/v1/chat/completions") {
            contentType(ContentType.Application.Json)
            setBody("""{"model":"qwen.gguf","messages":[{"role":"user","content":"hi"}]}""")
        }

        val hot = chat()
        assertEquals(HttpStatusCode.ServiceUnavailable, hot.status)
        assertEquals(
            "thermal_shedding",
            json.parseToJsonElement(hot.bodyAsText()).jsonObject.errorBody()["code"]!!.jsonPrimitive.content,
        )
        assertEquals("10", hot.headers["Retry-After"])
        assertContractHeaders(hot)

        val health = json.parseToJsonElement(client.get("/v1/health").bodyAsText()).jsonObject
        val resources = health["resources"]!!.jsonObject
        assertEquals("true", resources["thermal_shedding"]!!.jsonPrimitive.content)
        assertTrue(resources["shed_reason"]!!.jsonPrimitive.content.contains("thermal_status=3"))
        // 既有字段语义不变，新增字段反映有效并发
        assertEquals("2", health["limits"]!!.jsonObject["max_concurrency"]!!.jsonPrimitive.content)
        assertEquals("1", health["limits"]!!.jsonObject["effective_max_concurrency"]!!.jsonPrimitive.content)

        // 迟滞：46℃ 仍高于恢复阈值 45℃
        signal = ThermalSignal(statusLevel = 2, tempC = 46.0)
        assertEquals(HttpStatusCode.ServiceUnavailable, chat().status)

        signal = ThermalSignal(statusLevel = 2, tempC = 44.0)
        assertEquals(HttpStatusCode.OK, chat().status)
        val recovered = json.parseToJsonElement(client.get("/v1/health").bodyAsText()).jsonObject
        assertEquals("false", recovered["resources"]!!.jsonObject["thermal_shedding"]!!.jsonPrimitive.content)
    }

    @Test
    fun `热信号不可用时正常服务且不误判过热`() = testApplication {
        installTestApi(thermalProbe = { ThermalSignal() })

        val response = client.post("/v1/chat/completions") {
            contentType(ContentType.Application.Json)
            setBody("""{"model":"qwen.gguf","messages":[{"role":"user","content":"hi"}]}""")
        }
        assertEquals(HttpStatusCode.OK, response.status)
    }

    @Test
    fun `降载不中断已在途的流式请求`() = testApplication {
        var signal = ThermalSignal(statusLevel = 1, tempC = 30.0)
        installTestApi(engine = SlowLlmEngine(), thermalProbe = { signal })

        coroutineScope {
            val first = async {
                client.post("/v1/chat/completions") {
                    contentType(ContentType.Application.Json)
                    setBody("""{"model":"qwen.gguf","messages":[{"role":"user","content":"hi"}],"stream":true}""")
                }
            }
            delay(200) // 让首个请求进入流式生成
            signal = ThermalSignal(statusLevel = 3, tempC = 50.0)

            val second = client.post("/v1/chat/completions") {
                contentType(ContentType.Application.Json)
                setBody("""{"model":"qwen.gguf","messages":[{"role":"user","content":"hi"}]}""")
            }
            assertEquals(HttpStatusCode.ServiceUnavailable, second.status)

            val firstResponse = first.await()
            assertEquals(HttpStatusCode.OK, firstResponse.status)
            val text = firstResponse.bodyAsText()
            assertTrue(text.contains("你"))
            assertTrue(text.contains("[DONE]"))
        }
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
