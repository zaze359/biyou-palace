package com.biyou.sanxian.node

import com.biyou.sanxian.contract.ChatChoice
import com.biyou.sanxian.contract.ChatCompletion
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
import com.biyou.sanxian.contract.usageOf
import com.biyou.sanxian.llm.ChatMessage
import com.biyou.sanxian.llm.ChatSession
import com.biyou.sanxian.llm.GenerateStats
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
import io.ktor.server.plugins.statuspages.StatusPages
import io.ktor.server.request.receiveText
import io.ktor.server.response.ApplicationSendPipeline
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
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonPrimitive
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter

/**
 * 节点 HTTP 路由模块。
 *
 * 从 [NodeServer] 抽出，**不依赖 Android API**：健康数据由 [health] 回调提供，
 * 使得整条 HTTP 链路可以在 JVM 上用 Ktor `testApplication` 做端点级集成测试
 * （见 `androidUnitTest` 下的 NodeServerIntegrationTest），无需真机与真实模型。
 */
internal fun Application.installNodeApi(
    config: NodeConfig,
    engine: LlmEngine,
    embeddingEngine: EmbeddingEngine?,
    gate: ConcurrencyGate,
    health: suspend (queueLen: Int) -> NodeHealthCollector.CollectedHealth,
    governor: ThermalGovernor = ThermalGovernor(config),
    thermalProbe: () -> ThermalSignal = { ThermalSignal() },
) {
    // 日志脱敏需要一个稳定的 token 值：节点安装路由时登记，避免启动早于 /v1/logs 的事件漏脱敏。
    NodeLogBuffer.configure(config.sharedToken)
    NodeApi(config, engine, embeddingEngine, gate, health, governor, thermalProbe).configure(this)
}

/** 端点实现。行为与抽取前逐条一致；能力/鉴权/响应头语义见各类注释。 */
private class NodeApi(
    private val config: NodeConfig,
    private val engine: LlmEngine,
    private val embeddingEngine: EmbeddingEngine?,
    private val gate: ConcurrencyGate,
    private val health: suspend (queueLen: Int) -> NodeHealthCollector.CollectedHealth,
    private val governor: ThermalGovernor,
    private val thermalProbe: () -> ThermalSignal,
) {
    private val json = Json { encodeDefaults = true }

    fun configure(application: Application) {
        application.install(StatusPages) {
            exception<kotlinx.serialization.SerializationException> { call, e ->
                val (status, body) = badRequest("请求体格式错误：${e.message}").toOpenAIError()
                call.respondText(body, ContentType.Application.Json, HttpStatusCode.fromValue(status))
            }
            exception<LlmError> { call, e ->
                NodeLogBuffer.record("ERROR", "upstream", "推理引擎错误：${e.message}")
                val (status, body) = upstreamUnavailable("推理引擎错误：${e.message}").toOpenAIError()
                call.respondText(body, ContentType.Application.Json, HttpStatusCode.fromValue(status))
            }
            exception<NodeException> { call, e ->
                val (status, body) = e.toOpenAIError()
                if (status == 429) {
                    NodeLogBuffer.record("WARNING", "concurrency", e.message ?: "并发已达上限")
                    call.response.headers.append("Retry-After", "1")
                }
                if (status == 503 && e.code == "thermal_shedding") {
                    NodeLogBuffer.record("WARNING", "concurrency", "发热降载：拒绝新请求（${e.message}）")
                }
                e.retryAfterSeconds?.let { call.response.headers.append("Retry-After", it.toString()) }
                call.respondText(body, ContentType.Application.Json, HttpStatusCode.fromValue(status))
            }
            exception<Throwable> { call, e ->
                if (e is kotlinx.coroutines.CancellationException) throw e
                NodeLogBuffer.record("ERROR", "exception", "内部错误：${e.message}")
                val (_, body) =
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
                    // 只记录失败事实，不记录调用方提供的 token 值
                    NodeLogBuffer.record("WARNING", "auth", "鉴权失败：缺少或无效的 bearer token")
                    throw unauthorized("缺少或无效的 bearer token")
                }
            }
            proceed()
        }
        // 契约响应头：必须挂在发送管线的 Before 相位。CIO 在 Engine 相位就会把
        // headersBuilder 快照写进 HTTP 响应（含 SSE 与错误响应），此后再 append 不会生效。
        // 挂在 Before 是唯一 choke point：JSON、SSE 首帧、StatusPages 错误、路由 404 都会经过这里。
        application.sendPipeline.intercept(ApplicationSendPipeline.Phases.Before) {
            call.response.headers.append(Contract.HEADER_NODE_ID, config.nodeId)
            call.response.headers.append(Contract.HEADER_NODE_ROLE, Contract.ROLE)
            call.response.headers.append(Contract.HEADER_CONTRACT_VERSION, Contract.VERSION)
        }

        application.routing {
            get("/v1/health") {
                call.respondJson(
                    HttpStatusCode.OK,
                    json.encodeToString(HealthResponse.serializer(), buildHealth()),
                )
            }
            get("/v1/models") {
                call.respondJson(
                    HttpStatusCode.OK,
                    json.encodeToString(ModelList.serializer(), buildModels()),
                )
            }
            post("/v1/chat/completions") { handleChat(call) }
            post("/v1/embeddings") { handleEmbeddings(call) }
            get("/v1/logs") { handleLogs(call) }
        }
    }

    // ----------------------------- /v1/health -----------------------------

    private suspend fun buildHealth(): HealthResponse {
        val collected = health(gate.active)
        // health 采集到的是同源热信号，顺手刷新降载状态，保证面板与准入判断一致
        governor.update(ThermalSignal(collected.thermalStatusLevel, collected.tempC))
        val resources = collected.resources.copy(
            thermalShedding = governor.isShedding,
            shedReason = governor.shedReason,
        )
        val memPct = collected.memPct
        val cpuPct = collected.cpuPct
        val tempC = collected.tempC
        val info = runCatching { engine.getModelInfo() }.getOrNull()
        val engineLoaded = engine.isModelLoaded() && info != null
        // 单句柄引擎：已加载的必是 preload 首项。以「路径 basename」作为模型标识，
        // 使 capabilities_map 按文件名声明能与 loaded 状态对齐（避免列出幽灵条目）。
        // 注意：引擎暴露的 info.name 是 llama_model_desc（如 "Qwen3 0.6B"），并非文件名，
        // 因此这里统一用 basename 做身份，capabilities_map 按文件名声明即可精确命中。
        val loadedPath = if (engineLoaded) config.preloadModels.firstOrNull() else null
        val loadedIds = buildSet {
            loadedPath?.let { add(java.io.File(it).name) }
            // embedding 是独立句柄：配置里声明了不代表加载成功，必须问引擎真实状态
            if (embeddingEngine?.isLoaded == true) {
                config.embeddingModel?.takeIf { it.isNotBlank() }?.let { add(java.io.File(it).name) }
            }
        }
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
            limits = Limits(
                maxConcurrency = gate.limit,
                maxContextLen = knownContext.maxOrNull(),
                effectiveMaxConcurrency = governor.effectiveLimit,
            ),
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

        admitInference()
        if (req.stream) {
            gate.acquire(governor.effectiveLimit) // 满载立即 429（在进入流式前抢占名额）
            try {
                call.respondTextWriter(ContentType.Text.EventStream) {
                    val w = this // 捕获 Writer，供 launch 内部使用
                    coroutineScope {
                        // 首帧声明 assistant 角色（契约要求，客户端据此初始化消息气泡）
                        w.write(Sse.emit(Sse.roleBlock(id, req.model)))
                        w.flush()
                        val flow = engine.generate(session, prompt, req.maxTokens ?: 0)
                        val job = launch {
                            flow.collect { piece ->
                                try {
                                    w.write(Sse.emit(Sse.contentBlock(id, req.model, piece)))
                                    w.flush()
                                } catch (_: java.io.IOException) {
                                    NodeLogBuffer.record("INFO", "disconnect", "客户端断开连接，已停止生成")
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
            gate.acquire(governor.effectiveLimit)
            try {
                val sb = StringBuilder()
                val stats = GenerateStats()
                engine.generate(session, prompt, req.maxTokens ?: 0, stats).collect { sb.append(it) }
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
                            usage = usageOf(stats.promptTokens, stats.completionTokens),
                        )
                    )
                )
            } finally {
                gate.release()
            }
        }
    }

    // ----------------------------- /v1/logs -----------------------------

    /** 最近的运行事件（白名单环形缓冲，已脱敏）。limit 默认 50，夹到 1..200。 */
    private suspend fun handleLogs(call: ApplicationCall) {
        val raw = call.request.queryParameters["limit"] ?: "50"
        val limit = raw.toIntOrNull()
            ?: throw badRequest("limit 必须为整数，实际：$raw")
        call.respondJson(
            HttpStatusCode.OK,
            json.encodeToString(LogsResponse.serializer(), NodeLogBuffer.recent(limit)),
        )
    }

    // ----------------------------- /v1/embeddings -----------------------------

    private suspend fun handleEmbeddings(call: ApplicationCall) {
        val req = json.decodeFromString(EmbeddingRequest.serializer(), call.receiveText())
        // OpenAI 兼容：input 既可以是单个字符串，也可以是字符串数组
        val inputs = when (val e = req.input) {
            is JsonPrimitive -> listOf(e.content)
            is JsonArray -> e.map { it.jsonPrimitive.content }
            else -> throw badRequest("input 必须是字符串或字符串数组")
        }
        if (!config.supportsCapability(req.model, "embedding")) {
            throw capabilityNotSupported("模型 ${req.model.ifBlank { "(未指定)" }} 未声明 embedding 能力")
        }
        val eng = embeddingEngine
            ?: throw capabilityNotSupported("该节点未启用 embedding 后端（请在 native 层实现 embed 桥）")
        admitInference()
        gate.acquire(governor.effectiveLimit)
        try {
            val result = withContext(Dispatchers.IO) { eng.embed(inputs) }
            call.respondJson(
                HttpStatusCode.OK,
                json.encodeToString(
                    EmbeddingResponse.serializer(),
                    EmbeddingResponse(
                        model = req.model,
                        data = result.vectors.mapIndexed { i, v -> EmbeddingData(index = i, embedding = v) },
                        usage = usageOf(result.promptTokens, 0),
                    )
                )
            )
        } finally {
            gate.release()
        }
    }

    // ----------------------------- 请求 → 引擎会话 映射 -----------------------------

    /** 刷新热信号并做准入：降载期间新请求 503 + Retry-After，在途请求不受影响。 */
    private fun admitInference() {
        governor.update(thermalProbe())
        governor.admit()
    }

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
