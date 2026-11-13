package com.biyou.sanxian.contract

import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.descriptors.SerialDescriptor
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonDecoder
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonEncoder
import kotlinx.serialization.json.JsonPrimitive

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
    /** 是否处于发热降载（散仙专属；准圣恒为 null） */
    @SerialName("thermal_shedding") val thermalShedding: Boolean? = null,
    /** 降载触发原因（未降载为 null） */
    @SerialName("shed_reason") val shedReason: String? = null,
)

@Serializable
data class Limits(
    @SerialName("max_concurrency") val maxConcurrency: Int,
    @SerialName("max_context_len") val maxContextLen: Int?,
    /** 当前实际生效的并发上限：降载期间为 1（附加字段，max_concurrency 语义不变） */
    @SerialName("effective_max_concurrency") val effectiveMaxConcurrency: Int? = null,
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

/**
 * 消息内容：OpenAI 兼容的「字符串或内容数组」双形态。
 *
 * - 纯文本请求/响应始终序列化为 JSON 字符串，保持与旧客户端逐字节兼容；
 * - 多模态请求可传 `[{"type":"text",...},{"type":"image_url",...}]`，由 [ChatContent.Parts] 承载。
 */
@Serializable(with = ChatContentSerializer::class)
sealed interface ChatContent {
    /** 拼接所有 text 分片后的纯文本（图片分片忽略），供文本链路与日志使用。 */
    val plainText: String

    /** 按原始顺序提取图像分片；纯字符串形态恒为空。 */
    val imageUrls: List<ImageUrl>

    data class Text(val text: String) : ChatContent {
        override val plainText: String get() = text
        override val imageUrls: List<ImageUrl> get() = emptyList()
    }

    data class Parts(val parts: List<ContentPart>) : ChatContent {
        override val plainText: String get() = parts.mapNotNull { it.text }.joinToString("")

        override val imageUrls: List<ImageUrl> get() = parts.mapNotNull { it.imageUrl }
    }

    companion object {
        fun of(text: String): ChatContent = Text(text)
    }
}

@Serializable
data class ContentPart(
    val type: String,
    val text: String? = null,
    @SerialName("image_url") val imageUrl: ImageUrl? = null,
)

@Serializable
data class ImageUrl(
    /** `http(s)://...` 或 `data:image/<type>;base64,...` */
    val url: String,
    val detail: String? = null,
)

/**
 * 手工读写 `string | array`，不借助 JsonElement 的隐式多态。
 *
 * 之所以不用 `JsonElement` 作为 content 类型：响应侧（assistant 消息）必须输出字符串，
 * 若类型是 JsonElement，任何误构造都会让客户端拿到非契约 JSON。
 */
object ChatContentSerializer : KSerializer<ChatContent> {
    override val descriptor: SerialDescriptor = JsonElement.serializer().descriptor

    override fun deserialize(decoder: Decoder): ChatContent {
        val input = decoder as? JsonDecoder
            ?: throw SerializationException("ChatMessage.content 仅支持 JSON 解码")
        return when (val element = input.decodeJsonElement()) {
            is JsonPrimitive -> ChatContent.Text(element.content)
            is JsonArray -> ChatContent.Parts(
                element.map { input.json.decodeFromJsonElement(ContentPart.serializer(), it) }
            )
            else -> throw SerializationException("content 必须是字符串或内容数组")
        }
    }

    override fun serialize(encoder: Encoder, value: ChatContent) {
        val output = encoder as? JsonEncoder
            ?: throw SerializationException("ChatMessage.content 仅支持 JSON 编码")
        when (value) {
            is ChatContent.Text -> output.encodeString(value.text)
            is ChatContent.Parts -> output.encodeSerializableValue(
                ListSerializer(ContentPart.serializer()),
                value.parts,
            )
        }
    }
}

@Serializable
data class ChatMessage(
    val role: String,
    val content: ChatContent,
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

/**
 * 由真实 token 统计构造 usage。
 *
 * [promptTokens] 必须是**裁剪后**实际送入模型的 token 数（见 `GenerateStats`），
 * 不能回退成客户端原始输入的估算值，否则截断时 usage 会虚报。
 */
fun usageOf(promptTokens: Int, completionTokens: Int): Usage = Usage(
    promptTokens = promptTokens,
    completionTokens = completionTokens,
    totalTokens = promptTokens + completionTokens,
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
