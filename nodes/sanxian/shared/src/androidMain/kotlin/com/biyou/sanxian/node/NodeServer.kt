package com.biyou.sanxian.node

import android.content.Context
import com.biyou.sanxian.contract.ChatCompletion
import com.biyou.sanxian.contract.ChatChoice
import com.biyou.sanxian.contract.ChatMessage as ContractChatMessage
import com.biyou.sanxian.contract.ChatRequest
import com.biyou.sanxian.contract.EmbeddingData
import com.biyou.sanxian.contract.EmbeddingRequest
import com.biyou.sanxian.contract.EmbeddingResponse
import com.biyou.sanxian.contract.HealthResponse
import com.biyou.sanxian.contract.Limits
import com.biyou.sanxian.contract.ModelEntry
import com.biyou.sanxian.contract.ModelList
import com.biyou.sanxian.contract.ModelRef
import com.biyou.sanxian.contract.NodeMeta
import com.biyou.sanxian.contract.Usage
import com.biyou.sanxian.llm.ChatMessage
import com.biyou.sanxian.llm.ChatSession
import com.biyou.sanxian.llm.LlmEngine
import com.biyou.sanxian.llm.LlmError
import com.biyou.sanxian.llm.Role
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.server.application.Application
import io.ktor.server.application.ApplicationCall
import io.ktor.server.application.ApplicationCallPipeline
import io.ktor.server.application.call
import io.ktor.server.application.install
import io.ktor.server.engine.embeddedServer
import io.ktor.server.cio.CIO
import io.ktor.server.plugins.statuspages.StatusPages
import io.ktor.server.request.receiveText
import io.ktor.server.response.respondText
import io.ktor.server.response.respondTextWriter
import io.ktor.server.routing.get
import io.ktor.server.routing.post
import io.ktor.server.routing.routing
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter
import io.ktor.util.pipeline.PipelinePhase

/** 自定义相位：在发送管线末尾追加节点契约响应头（X-Node-Id / X-Node-Role / Contract-Version）。 */
private val ResponseHeadersPhase = PipelinePhase("ResponseHeaders")

/**
 * 散仙节点 HTTP 服务（Ktor/CIO）。与准圣（FastAPI）对外行为**逐条一致**，仅推理后端不同：
 * 准圣代理 Ollama；散仙桥接本地 llama.cpp（[LlmEngine]）。
 *
 * 端点（契约 v1.0）：
 *  - GET  /v1/health       原生拼装（Ollama 无此端点）
 *  - GET  /v1/models       列出本地模型（OpenAI 兼容）
 *  - POST /v1/chat/completions  桥接 LlmEngine.generate(): Flow<String> → SSE 自产
 *  - POST /v1/embeddings   能力驱动；无 embedding 能力 → 501；否则走 [EmbeddingEngine]
 *
 * 切面：响应头（X-Node-Id / X-Node-Role / Contract-Version）、可选 bearer 鉴权、
 * 并发闸门（满载 429 + Retry-After）、客户端断连即 [LlmEngine.stopGeneration]。
 */
class NodeServer(
    private val context: Context,
    private val config: NodeConfig,
    private val engine: LlmEngine,
    private val embeddingEngine: EmbeddingEngine?,
) {
    private val json = Json { encodeDefaults = true }
    private val gate = ConcurrencyGate(config.maxConcurrency)
    private val server = embeddedServer(CIO, port = config.port, host = "0.0.0.0") {
        configure(this)
    }

    private fun configure(application: Application) {
        application.install(StatusPages) {
            exception<kotlinx.serialization.SerializationException> { call, e ->
                val (status, body) = badRequest("请求体格式错误：${e.message}").toOpenAIError()
                call.respondText(body, ContentType.Application.Json, HttpStatusCode.fromValue(status))
            }
            exception<LlmError> { call, e ->
                val (status, body) = upstreamUnavailable("推理引擎错误：${e.message}").toOpenAIError()
                call.respondText(body, ContentType.Application.Json, HttpStatusCode.fromValue(status))
            }
            exception<NodeException> { call, e ->
                val (status, body) = e.toOpenAIError()
                if (status == 429) call.response.headers.append("Retry-After", "1")
                call.respondText(body, ContentType.Application.Json, HttpStatusCode.fromValue(status))
            }
            exception<Throwable> { call, e ->
                if (e is kotlinx.coroutines.CancellationException) throw e
                val (status, body) =
                    NodeException("内部错误：${e.message}", 500, "server_error", "internal_error").toOpenAIError()
                call.respondText(body, ContentType.Application.Json, HttpStatusCode.InternalServerError)
            }
        }

        // 鉴权 + 响应头 中间件
        application.intercept(ApplicationCallPipeline.Call) {
            if (config.authEnabled && !config.sharedToken.isNullOrBlank()) {
                val auth = call.request.headers[HttpHeaders.Authorization].orEmpty()
                val (scheme, token) = auth.split(" ", limit = 2).let {
                    (if (it.size == 2) it[0] else "") to (if (it.size == 2) it[1] else "")
                }
                if (!(scheme.equals("Bearer", ignoreCase = true) && token == config.sharedToken)) {
                    throw unauthorized("缺少或无效的 bearer token")
                }
            }
            proceed()
        }
        application.sendPipeline.addPhase(ResponseHeadersPhase)
        application.sendPipeline.intercept(ResponseHeadersPhase) {
            call.response.headers.append(Contract.HEADER_NODE_ID, config.nodeId)
            call.response.headers.append(Contract.HEADER_NODE_ROLE, Contract.ROLE)
            call.response.headers.append(Contract.HEADER_CONTRACT_VERSION, Contract.VERSION)
        }

        application.routing {
            get("/v1/health") { call.respondJson(HttpStatusCode.OK, json.encodeToString(HealthResponse.serializer(), buildHealth())) }
            get("/v1/models") { call.respondJson(HttpStatusCode.OK, json.encodeToString(ModelList.serializer(), buildModels())) }
            post("/v1/chat/completions") { handleChat(call) }
            post("/v1/embeddings") { handleEmbeddings(call) }
        }
    }

    // ----------------------------- 启动 / 停止 -----------------------------

    fun start(wait: Boolean = false) = server.start(wait)
    fun stop(gracePeriod: Long = 1000, timeout: Long = 2000) = server.stop(gracePeriod, timeout)

    // ----------------------------- /v1/health -----------------------------

    private suspend fun buildHealth(): HealthResponse {
        val (resources, memPct, cpuPct, tempC) = NodeHealthCollector.collect(context, gate.active)
        val info = runCatching { engine.getModelInfo() }.getOrNull()
        val engineLoaded = engine.isModelLoaded() && info != null
        // 单句柄引擎：已加载的必是 preload 首项。以「路径 basename」作为模型标识，
        // 使 capabilities_map 按文件名声明能与 loaded 状态对齐（避免列出幽灵条目）。
        // 注意：引擎暴露的 info.name 是 llama_model_desc（如 "Qwen3 0.6B"），并非文件名，
        // 因此这里统一用 basename 做身份，capabilities_map 按文件名声明即可精确命中。
        val loadedPath = if (engineLoaded) config.preloadModels.firstOrNull() else null
        val loadedIds = loadedPath?.let { setOf(java.io.File(it).name) } ?: emptySet()
        val engineOk = true // 引擎单例已构造（libllama_jni 已 load）；真正不可用由 generate→LlmError→503
        val preloadOk = NodeStatus.preloadSatisfied(
            config.preloadModels, loadedPath?.let { setOf(it) } ?: emptySet())
        val status = NodeStatus.determine(engineOk, preloadOk, memPct, cpuPct, tempC, config)
        val names = (config.capabilitiesMap.keys + loadedIds).toSet()
        val models = names.map { name ->
            ModelEntry(
                name = name,
                loaded = name in loadedIds,
                capabilities = config.capabilityFor(name),
                contextLen = config.contextLenFor(name),
            )
        }
        val knownContext = models.mapNotNull { it.contextLen }
        return HealthResponse(
            node = NodeMeta(
                id = config.nodeId,
                name = config.nodeName,
                role = Contract.ROLE,
                arch = Contract.ARCH,
                runtime = Contract.RUNTIME,
                runtimeVersion = info?.backend,
                contractVersion = Contract.VERSION,
            ),
            status = status,
            models = models,
            resources = resources,
            limits = Limits(maxConcurrency = gate.limit, maxContextLen = knownContext.maxOrNull()),
            updatedAt = OffsetDateTime.now().format(DateTimeFormatter.ISO_OFFSET_DATE_TIME),
        )
    }

    // ----------------------------- /v1/models -----------------------------

    private suspend fun buildModels(): ModelList {
        val engineLoaded = engine.isModelLoaded()
        val loadedIds = if (engineLoaded) {
            config.preloadModels.firstOrNull()?.let { setOf(java.io.File(it).name) } ?: emptySet()
        } else emptySet()
        val names = (config.capabilitiesMap.keys + loadedIds).toSet()
        return ModelList(data = names.map { ModelRef(id = it, ownedBy = "biyou-node") })
    }

    // ----------------------------- /v1/chat/completions -----------------------------

    private suspend fun handleChat(call: ApplicationCall) {
        val req = json.decodeFromString(ChatRequest.serializer(), call.receiveText())
        val id = "chatcmpl-sanxian-${System.currentTimeMillis()}"
        val session = buildSession(req)
        val prompt = lastUserContent(req)

        if (req.stream) {
            gate.acquire() // 满载立即 429（在进入流式前抢占名额）
            try {
                call.respondTextWriter(ContentType.Text.EventStream) {
                    val w = this // 捕获 Writer，供 launch 内部使用
                    coroutineScope {
                        val flow = engine.generate(session, prompt)
                        val job = launch {
                            flow.collect { piece ->
                                try {
                                    w.write(Sse.emit(Sse.contentBlock(id, req.model, piece)))
                                    w.flush()
                                } catch (_: java.io.IOException) {
                                    cancel() // 客户端断开：取消收集 → 触发 engine finally 调 stopGeneration
                                    return@collect
                                }
                            }
                        }
                        job.join()
                        if (!job.isCancelled) {
                            w.write(Sse.emit(Sse.stopBlock(id, req.model)))
                            w.flush()
                            w.write(Sse.emit(Sse.DONE))
                            w.flush()
                        }
                    }
                }
            } finally {
                gate.release()
            }
        } else {
            gate.acquire()
            try {
                val sb = StringBuilder()
                engine.generate(session, prompt).collect { sb.append(it) }
                val content = sb.toString()
                call.respondJson(
                    HttpStatusCode.OK,
                    json.encodeToString(
                        ChatCompletion.serializer(),
                        ChatCompletion(
                            id = id,
                            model = req.model,
                            choices = listOf(
                                ChatChoice(
                                    index = 0,
                                    message = ContractChatMessage(role = "assistant", content = content),
                                    finishReason = "stop",
                                )
                            ),
                            usage = Usage(
                                promptTokens = (req.messages.sumOf { it.content.length } / 4),
                                completionTokens = (content.length / 4),
                                totalTokens = (req.messages.sumOf { it.content.length } / 4) + (content.length / 4),
                            ),
                        )
                    )
                )
            } finally {
                gate.release()
            }
        }
    }

    // ----------------------------- /v1/embeddings -----------------------------

    private suspend fun handleEmbeddings(call: ApplicationCall) {
        val req = json.decodeFromString(EmbeddingRequest.serializer(), call.receiveText())
        if ("embedding" !in config.capabilityFor(req.model)) {
            throw capabilityNotSupported("模型 ${req.model.ifBlank { "(未指定)" }} 未声明 embedding 能力")
        }
        val eng = embeddingEngine
            ?: throw capabilityNotSupported("该节点未启用 embedding 后端（请在 native 层实现 embed 桥）")
        gate.acquire()
        try {
            val vectors = withContext(Dispatchers.IO) { eng.embed(req.input) }
            call.respondJson(
                HttpStatusCode.OK,
                json.encodeToString(
                    EmbeddingResponse.serializer(),
                    EmbeddingResponse(
                        model = req.model,
                        data = vectors.mapIndexed { i, v -> EmbeddingData(index = i, embedding = v) },
                        usage = Usage(
                            promptTokens = req.input.sumOf { it.length },
                            completionTokens = 0,
                            totalTokens = req.input.sumOf { it.length },
                        ),
                    )
                )
            )
        } finally {
            gate.release()
        }
    }

    // ----------------------------- 请求 → 引擎会话 映射 -----------------------------

    private fun buildSession(req: ChatRequest): ChatSession {
        val system = req.messages.firstOrNull { it.role == "system" }?.content
        val history = req.messages
            .filter { it.role != "system" }
            .dropLast(1)
            .map { ChatMessage(role = roleOf(it.role), content = it.content) }
        return ChatSession(messages = history, systemPrompt = system)
    }

    private fun lastUserContent(req: ChatRequest): String =
        req.messages.lastOrNull { it.role == "user" }?.content ?: ""

    private fun roleOf(role: String): Role = when (role.lowercase()) {
        "assistant" -> Role.Assistant
        "system" -> Role.System
        else -> Role.User
    }

    private suspend fun ApplicationCall.respondJson(status: HttpStatusCode, jsonString: String) {
        respondText(jsonString, ContentType.Application.Json, status)
    }
}
