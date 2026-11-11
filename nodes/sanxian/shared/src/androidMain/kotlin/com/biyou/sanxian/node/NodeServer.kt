package com.biyou.sanxian.node

import android.content.Context
import com.biyou.sanxian.llm.LlmEngine
import io.ktor.server.cio.CIO
import io.ktor.server.engine.embeddedServer

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
 *
 * 路由实现见 [installNodeApi]（不含 Android 依赖，便于 JVM 端点级集成测试）；
 * 本类只负责绑定端口、把 Android 资源采集注入 [installNodeApi]。
 */
class NodeServer(
    context: Context,
    private val config: NodeConfig,
    private val engine: LlmEngine,
    private val embeddingEngine: EmbeddingEngine?,
) {
    private val gate = ConcurrencyGate(config.maxConcurrency)
    private val governor = ThermalGovernor(config)
    private val server = embeddedServer(CIO, port = config.port, host = "0.0.0.0") {
        installNodeApi(
            config, engine, embeddingEngine, gate,
            health = { queueLen -> NodeHealthCollector.collect(context, queueLen) },
            governor = governor,
            thermalProbe = { NodeHealthCollector.readThermalSignal(context) },
        )
    }

    fun start(wait: Boolean = false) = server.start(wait)
    fun stop(gracePeriod: Long = 1000, timeout: Long = 2000) = server.stop(gracePeriod, timeout)
}
