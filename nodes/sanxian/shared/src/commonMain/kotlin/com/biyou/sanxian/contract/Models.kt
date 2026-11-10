package com.biyou.sanxian.contract

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

/**
 * 节点智能体统一契约（contract v1.0）的 Kotlin 数据模型。
 *
 * 与 [docs/node-agent-contract.md] 逐字段对齐；准圣（Python/Ollama）已实现并实跑验证，
 * 本模块是其 Kotlin 镜像——保证两套智能体对外 HTTP 行为完全一致（协议级共享，非代码共享）。
 *
 * 序列化用 kotlinx.serialization；SSE 块为手工拼装（见 [com.biyou.sanxian.node.Sse]），
 * 不走序列化，以精确控制 `null` 字段与 `delta:{}` 末块的呈现。
 */

// ----------------------------- /v1/health -----------------------------

@Serializable
data class HealthResponse(
    val node: NodeMeta,
    val status: String,
    val models: List<ModelEntry>,
    val resources: Resources,
    val limits: Limits,
    @SerialName("updated_at") val updatedAt: String,
)

@Serializable
data class NodeMeta(
    val id: String,
    val name: String,
    val role: String,          // "sanxian" | "quasheng"
    val arch: String,          // "arm64"
    val runtime: String,       // "llama.cpp" | "ollama"
    @SerialName("runtime_version") val runtimeVersion: String?,
    @SerialName("contract_version") val contractVersion: String,
)

@Serializable
data class ModelEntry(
    val name: String,
    val family: String? = null,
    @SerialName("size_label") val sizeLabel: String? = null,
    @SerialName("context_len") val contextLen: Int? = null,
    val loaded: Boolean,
    val capabilities: List<String>,
)

@Serializable
data class Resources(
    @SerialName("cpu_pct") val cpuPct: Double?,
    @SerialName("mem_used_mb") val memUsedMb: Long?,
    @SerialName("mem_total_mb") val memTotalMb: Long?,
    @SerialName("disk_free_mb") val diskFreeMb: Long?,
    @SerialName("queue_len") val queueLen: Int,
    // 散仙专属真实字段；非安卓实现（如未来桌面）置 null
    @SerialName("battery_pct") val batteryPct: Double? = null,
    @SerialName("temperature_c") val temperatureC: Double? = null,
    @SerialName("thermal_state") val thermalState: String? = null,
)

@Serializable
data class Limits(
    @SerialName("max_concurrency") val maxConcurrency: Int,
    @SerialName("max_context_len") val maxContextLen: Int?,
)

// ----------------------------- /v1/models -----------------------------

@Serializable
data class ModelList(
    val `object`: String = "list",
    val data: List<ModelRef>,
)

@Serializable
data class ModelRef(
    val id: String,
    val `object`: String = "model",
    @SerialName("owned_by") val ownedBy: String,
)

// ----------------------------- /v1/chat/completions -----------------------------

@Serializable
data class ChatRequest(
    val model: String,
    val messages: List<ChatMessage>,
    val stream: Boolean = false,
    val temperature: Double = 0.7,
    @SerialName("max_tokens") val maxTokens: Int? = null,
)

@Serializable
data class ChatMessage(
    val role: String,
    val content: String,
)

@Serializable
data class ChatCompletion(
    val id: String,
    val `object`: String = "chat.completion",
    val model: String,
    val choices: List<ChatChoice>,
    val usage: Usage,
)

@Serializable
data class ChatChoice(
    val index: Int,
    val message: ChatMessage,
    @SerialName("finish_reason") val finishReason: String,
)

@Serializable
data class Usage(
    @SerialName("prompt_tokens") val promptTokens: Int,
    @SerialName("completion_tokens") val completionTokens: Int,
    @SerialName("total_tokens") val totalTokens: Int,
)

// ----------------------------- /v1/embeddings -----------------------------

@Serializable
data class EmbeddingRequest(
    val model: String,
    /**
     * OpenAI 兼容：既接受单个字符串，也接受字符串数组。
     * 字符串在服务层归一化为单元素列表。
     */
    val input: JsonElement,
)

@Serializable
data class EmbeddingResponse(
    val `object`: String = "list",
    val model: String,
    val data: List<EmbeddingData>,
    val usage: Usage,
)

@Serializable
data class EmbeddingData(
    val `object`: String = "embedding",
    val index: Int,
    val embedding: List<Float>,
)

// ----------------------------- 错误体（OpenAI 风格）-----------------------------

@Serializable
data class OpenAIError(
    val error: OpenAIErrorBody,
)

@Serializable
data class OpenAIErrorBody(
    val message: String,
    val type: String,
    val param: String? = null,
    val code: String,
)
