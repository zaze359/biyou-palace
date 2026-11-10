package com.biyou.sanxian.node

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * 散仙节点配置。与准圣 [Settings] 同构，差异点：无 ollama_base（本地引擎），新增
 * `tempWarnC`（散仙专属高温阈值，用于 `thermal_state` / `degraded` 判定）。
 *
 * 配置来源：Assets 中的 `sanxian.json`（见 [loadNodeConfig]），由前台服务启动时读取。
 * 本类同时支持 `@Serializable`（JSON 资产反序列化）与 `fromMap`（扁平键值，供 YAML/DataStore）。
 */
@Serializable
data class NodeConfig(
    @SerialName("node_id") val nodeId: String = "sanxian-01",
    @SerialName("node_name") val nodeName: String = "散仙·01",
    val port: Int = 4011,
    /** 可选 bearer 鉴权；非空即开启，覆盖全部端点（含 /v1/health） */
    @SerialName("shared_token") val sharedToken: String? = null,
    @SerialName("auth_enabled") val authEnabled: Boolean = false,
    /** 启动即预热常驻的模型（对应准圣 preload_models warmup），消除首请求阻塞 */
    @SerialName("preload_models") val preloadModels: List<String> = emptyList(),
    /**
     * 独立 embedding 模型路径（Plan A）。非空时节点会额外加载该模型到独立 embedding 句柄，
     * 并向集群声明该文件的 `embedding` 能力（见 [capabilitiesMap]）。为空则不承接 embedding。
     */
    @SerialName("embedding_model") val embeddingModel: String? = null,
    /** 能力声明：未登记的模型默认只有 chat */
    @SerialName("capabilities_map") val capabilitiesMap: Map<String, ModelCapability> = emptyMap(),
    /** 并发上限（手机资源受限，建议 1~2），满载立即 429 */
    @SerialName("max_concurrency") val maxConcurrency: Int = 2,
    @SerialName("mem_warn_pct") val memWarnPct: Double = 85.0,
    @SerialName("cpu_warn_pct") val cpuWarnPct: Double = 90.0,
    /** 温度预警阈值（摄氏）；超过即 degraded（thermal_state=hot） */
    @SerialName("temp_warn_c") val tempWarnC: Double = 45.0,
    /** 单次推理 / 嵌入的请求超时（毫秒） */
    @SerialName("request_timeout_ms") val requestTimeoutMs: Long = 300_000,
) {
    fun capabilityFor(model: String): List<String> {
        val entry = capabilitiesMap[model]
        return if (entry != null && entry.capabilities.isNotEmpty()) entry.capabilities else listOf("chat")
    }

    fun contextLenFor(model: String): Int? = capabilitiesMap[model]?.contextLen

    /** 能力判定入口：未登记能力的模型按 chat-only 处理（见 [capabilityFor]）。 */
    fun supportsCapability(model: String, capability: String): Boolean =
        capability in capabilityFor(model)
}

/**
 * 用户在设备端（DataStore）保存的覆盖项。
 *
 * 每个字段为 `null` 表示「用户没保存过该项」，沿用 assets 默认值；
 * 模型字段另有约定：`""` 表示显式不启用，非空表示模型文件名。
 * 该类型是纯数据，便于在 commonTest 中回归「用户配置 > assets」的合并优先级。
 */
data class UserConfigOverrides(
    val port: Int? = null,
    val sharedToken: String? = null,
    val authEnabled: Boolean? = null,
    val maxConcurrency: Int? = null,
    val chatModel: String? = null,
    val embeddingModel: String? = null,
)

/**
 * 合并用户覆盖项：**用户配置 > assets 默认值**。
 *
 * 同时补齐能力声明：用户选了 assets 里没登记过的 chat 模型时补 `chat`，
 * 选中的 embedding 模型强制声明 `embedding`（否则 /v1/embeddings 会因未声明能力被 501）。
 */
fun NodeConfig.withUserOverrides(overrides: UserConfigOverrides?): NodeConfig {
    if (overrides == null) return this
    val nextChatModels = overrides.chatModel?.let { name ->
        if (name.isBlank()) emptyList() else listOf(name)
    } ?: preloadModels
    // 注意：null=未覆盖，""=显式关闭；不能用 `?.let { it.ifBlank { null } } ?: embeddingModel`，
    // 否则显式关闭会被 assets 默认值吞掉（该回归由 commonTest 覆盖）。
    val nextEmbedding = when {
        overrides.embeddingModel == null -> embeddingModel
        overrides.embeddingModel.isBlank() -> null
        else -> overrides.embeddingModel
    }

    val capabilities = capabilitiesMap.toMutableMap()
    nextChatModels.firstOrNull()?.let { name ->
        if (capabilities[name] == null) capabilities[name] = ModelCapability(listOf("chat"))
    }
    nextEmbedding?.let { name ->
        capabilities[name] = ModelCapability(listOf("embedding"), capabilities[name]?.contextLen)
    }

    return copy(
        port = overrides.port ?: port,
        sharedToken = overrides.sharedToken?.takeIf { it.isNotBlank() } ?: sharedToken,
        authEnabled = overrides.authEnabled ?: authEnabled,
        maxConcurrency = overrides.maxConcurrency ?: maxConcurrency,
        preloadModels = nextChatModels,
        embeddingModel = nextEmbedding,
        capabilitiesMap = capabilities,
    )
}

@Serializable
data class ModelCapability(
    val capabilities: List<String>,
    @SerialName("context_len") val contextLen: Int? = null,
)

/** 从 JSON 资产（sanxian.json）反序列化；失败回退默认配置。 */
fun loadNodeConfig(jsonText: String?): NodeConfig {
    if (jsonText.isNullOrBlank()) return NodeConfig()
    return runCatching {
        kotlinx.serialization.json.Json { ignoreUnknownKeys = true }
            .decodeFromString(NodeConfig.serializer(), jsonText)
    }.getOrElse { NodeConfig() }
}

/**
 * 从键值对（Assets YAML / DataStore 解析后的扁平结构）构造 [NodeConfig]。
 * 仅识别已知字段；未知字段忽略。与准圣 `load_settings` 的「环境变量 > YAML > 默认」一致。
 */
fun NodeConfig.Companion.fromMap(raw: Map<String, Any?>): NodeConfig {
    fun str(k: String) = (raw[k] as? String)?.takeIf { it.isNotBlank() }
    fun int(k: String) = (raw[k] as? Number)?.toInt()
    fun dbl(k: String) = (raw[k] as? Number)?.toDouble()
    fun bool(k: String) = raw[k] as? Boolean

    val capRaw = raw["capabilities_map"] as? Map<*, *>
    val capabilities = capRaw?.mapNotNull { (k, v) ->
        val name = k.toString()
        when (v) {
            is List<*> -> name to ModelCapability(v.filterIsInstance<String>())
            is Map<*, *> -> name to ModelCapability(
                (v["capabilities"] as? List<*>)?.filterIsInstance<String>() ?: listOf("chat"),
                (v["context_len"] as? Number)?.toInt(),
            )
            else -> null
        }
    }?.toMap().orEmpty()

    val token = str("shared_token")
    val auth = bool("auth_enabled") ?: (token != null)

    return NodeConfig(
        nodeId = str("node_id") ?: "sanxian-01",
        nodeName = str("node_name") ?: "散仙·01",
        port = int("port") ?: 4011,
        sharedToken = token,
        authEnabled = auth,
        preloadModels = (raw["preload_models"] as? List<*>)?.filterIsInstance<String>() ?: emptyList(),
        embeddingModel = str("embedding_model"),
        capabilitiesMap = capabilities,
        maxConcurrency = int("max_concurrency") ?: 2,
        memWarnPct = dbl("mem_warn_pct") ?: 85.0,
        cpuWarnPct = dbl("cpu_warn_pct") ?: 90.0,
        tempWarnC = dbl("temp_warn_c") ?: 45.0,
        requestTimeoutMs = (int("request_timeout_ms") ?: 300_000).toLong(),
    )
}
