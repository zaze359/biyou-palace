package com.biyou.sanxian.ui

import com.biyou.sanxian.contract.ChatMessage as ContractChatMessage
import com.biyou.sanxian.contract.ChatRequest
import com.biyou.sanxian.contract.HealthResponse
import io.ktor.client.HttpClient
import io.ktor.client.engine.cio.CIO
import io.ktor.client.request.accept
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.client.request.preparePost
import io.ktor.client.request.setBody
import io.ktor.client.statement.bodyAsChannel
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.contentType
import io.ktor.utils.io.readUTF8Line
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/** 本机节点 HTTP 端点（契约原则 2：客户端只走节点 HTTP 接口，不直连引擎）。 */
data class NodeChatEndpoint(val baseUrl: String, val token: String?)

/** 节点侧模型状态（来自 `/v1/health`，不读写本地引擎）。 */
data class NodeModelStatus(
    val model: String?,
    val nodeStatus: String?,
    val error: String? = null,
)

/** 节点返回非 2xx 时抛出，供 UI 呈现状态码与 OpenAI 错误码。 */
class NodeChatException(
    val status: Int,
    val code: String?,
    message: String,
) : Exception(message)

/**
 * 聊天用的节点 HTTP 客户端。
 *
 * 抽象成接口是为了让 [ChatViewModel] 不依赖网络实现即可单测，
 * 同时从类型上杜绝聊天层再次接触 [com.biyou.sanxian.llm.LlmEngine]。
 */
interface NodeChatClient {
    /** 读取节点当前已加载的 chat 模型；节点不可达时抛异常。 */
    suspend fun fetchModelStatus(endpoint: NodeChatEndpoint): NodeModelStatus

    /** 流式对话：每收到一段增量回调一次；正常返回即流结束（[DONE] 或 EOF）。 */
    suspend fun streamChat(
        endpoint: NodeChatEndpoint,
        model: String,
        messages: List<ContractChatMessage>,
        onDelta: suspend (String) -> Unit,
    )

    fun close()
}

/** Ktor 实现：请求本机 `http://127.0.0.1:<port>/v1`，解析 SSE。 */
class KtorNodeChatClient(
    private val http: HttpClient = HttpClient(CIO),
) : NodeChatClient {

    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    override suspend fun fetchModelStatus(endpoint: NodeChatEndpoint): NodeModelStatus {
        val response = http.get("${endpoint.baseUrl}/health") { applyAuth(endpoint) }
        if (response.status != HttpStatusCode.OK) {
            throw NodeChatException(response.status.value, null, "节点返回 HTTP ${response.status.value}")
        }
        val health = json.decodeFromString(HealthResponse.serializer(), response.bodyAsText())
        val model = health.models.firstOrNull { it.loaded && "chat" in it.capabilities }?.name
        return NodeModelStatus(
            model = model,
            nodeStatus = health.status,
            error = if (model == null) "节点当前没有已加载的 chat 模型" else null,
        )
    }

    override suspend fun streamChat(
        endpoint: NodeChatEndpoint,
        model: String,
        messages: List<ContractChatMessage>,
        onDelta: suspend (String) -> Unit,
    ) {
        val body = json.encodeToString(
            ChatRequest.serializer(),
            ChatRequest(model = model, messages = messages, stream = true),
        )
        http.preparePost("${endpoint.baseUrl}/chat/completions") {
            applyAuth(endpoint)
            contentType(ContentType.Application.Json)
            accept(ContentType.Text.EventStream)
            setBody(body)
        }.execute { response ->
            if (response.status != HttpStatusCode.OK) {
                throw response.toChatException()
            }
            val channel = response.bodyAsChannel()
            while (true) {
                val line = channel.readUTF8Line() ?: break
                if (!line.startsWith("data:")) continue
                val payload = line.removePrefix("data:").trim()
                if (payload == "[DONE]") break
                val delta = parseDelta(payload) ?: continue
                if (delta.isNotEmpty()) onDelta(delta)
            }
        }
    }

    override fun close() = http.close()

    private fun io.ktor.client.request.HttpRequestBuilder.applyAuth(endpoint: NodeChatEndpoint) {
        // token 来自设备配置（DataStore > assets）；节点未开鉴权时服务端会忽略该头。
        endpoint.token?.takeIf { it.isNotBlank() }?.let {
            header(HttpHeaders.Authorization, "Bearer $it")
        }
    }

    private suspend fun io.ktor.client.statement.HttpResponse.toChatException(): NodeChatException {
        val text = runCatching { bodyAsText() }.getOrDefault("")
        val error = runCatching {
            json.parseToJsonElement(text).jsonObject["error"]?.jsonObject
        }.getOrNull()
        val message = error?.get("message")?.jsonPrimitive?.contentOrNull
            ?: "节点返回 HTTP ${status.value}"
        return NodeChatException(status.value, error?.get("code")?.jsonPrimitive?.contentOrNull, message)
    }

    /** 只取 `choices[0].delta.content`；role/stop 等无 content 的帧返回 null。 */
    private fun parseDelta(payload: String): String? {
        val obj: JsonObject = runCatching { json.parseToJsonElement(payload).jsonObject }.getOrNull()
            ?: return null
        val delta = obj["choices"]?.jsonArray?.firstOrNull()?.jsonObject
            ?.get("delta")?.jsonObject
            ?: return null
        return delta["content"]?.jsonPrimitive?.contentOrNull
    }
}
