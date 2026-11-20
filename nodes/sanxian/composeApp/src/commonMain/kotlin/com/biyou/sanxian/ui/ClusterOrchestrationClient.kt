package com.biyou.sanxian.ui

import com.biyou.sanxian.orchestration.RequestEnvelope
import com.biyou.sanxian.orchestration.ResultEnvelope
import com.biyou.sanxian.orchestration.TaskEvent
import io.ktor.client.HttpClient
import io.ktor.client.engine.cio.CIO
import io.ktor.client.request.accept
import io.ktor.client.request.header
import io.ktor.client.request.prepareGet
import io.ktor.client.request.preparePost
import io.ktor.client.request.setBody
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsChannel
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.contentType
import io.ktor.utils.io.readUTF8Line
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/** 阵图连接参数；[nodeId] 会作为 `X-Node-Id` 头在共享 token 模式下标识身份。 */
data class ZhentuEndpoint(
    val baseUrl: String,
    val token: String?,
    val nodeId: String,
)

/** `POST /zhentu/v1/requests` 的响应（202 新建；200 为幂等重复）。 */
data class ClusterSubmit(
    val taskId: String,
    val eventsUrl: String,
    val resultUrl: String,
    val duplicate: Boolean,
)

/** 阵图返回非 2xx 或协议异常；[status]=0 表示网络/解析层面的失败。 */
class ClusterException(
    val status: Int,
    val code: String?,
    message: String,
) : Exception(message) {
    /** 提交前失败（未创建任务）才允许 auto 回退本地。 */
    val beforeTaskCreated: Boolean
        get() = status == 0 || status >= 500 || (status in 400..499 && status != 410)
}

/**
 * 阵图任务客户端：提交、SSE 事件、取结果、取消。
 *
 * 只做协议转发，不做规划/选路/聚合；事件与结果直接交给 [ChatViewModel] 渲染。
 */
interface ClusterOrchestrationClient {
    suspend fun submit(endpoint: ZhentuEndpoint, envelope: RequestEnvelope): ClusterSubmit

    /** 订阅任务事件；[lastEventId] 非空时随请求发送 `Last-Event-ID` 续传。 */
    suspend fun streamEvents(
        endpoint: ZhentuEndpoint,
        eventsUrl: String,
        lastEventId: String?,
        onEvent: suspend (TaskEvent) -> Unit,
    )

    suspend fun getResult(endpoint: ZhentuEndpoint, resultUrl: String): ResultEnvelope

    suspend fun cancel(endpoint: ZhentuEndpoint, taskId: String)

    fun close()
}

class KtorClusterOrchestrationClient(
    private val http: HttpClient = HttpClient(CIO),
) : ClusterOrchestrationClient {

    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    override suspend fun submit(endpoint: ZhentuEndpoint, envelope: RequestEnvelope): ClusterSubmit {
        return http.preparePost("${apiBase(endpoint)}/requests") {
            applyAuth(endpoint)
            contentType(ContentType.Application.Json)
            setBody(json.encodeToString(RequestEnvelope.serializer(), envelope))
        }.execute { response ->
            if (response.status != HttpStatusCode.Accepted && response.status != HttpStatusCode.OK) {
                throw response.toClusterException()
            }
            val body = json.parseToJsonElement(response.bodyAsText()).jsonObject
            val taskId = body["task_id"]?.jsonPrimitive?.contentOrNull
                ?: throw ClusterException(0, "invalid_response", "阵图响应缺少 task_id")
            ClusterSubmit(
                taskId = taskId,
                eventsUrl = body["events_url"]?.jsonPrimitive?.contentOrNull
                    ?: "/zhentu/v1/requests/$taskId/events",
                resultUrl = body["result_url"]?.jsonPrimitive?.contentOrNull
                    ?: "/zhentu/v1/requests/$taskId",
                duplicate = body["duplicate"]?.jsonPrimitive?.contentOrNull == "true",
            )
        }
    }

    override suspend fun streamEvents(
        endpoint: ZhentuEndpoint,
        eventsUrl: String,
        lastEventId: String?,
        onEvent: suspend (TaskEvent) -> Unit,
    ) {
        http.prepareGet(resolveUrl(endpoint.baseUrl, eventsUrl)) {
            applyAuth(endpoint)
            accept(ContentType.Text.EventStream)
            lastEventId?.takeIf { it.isNotBlank() }?.let {
                header("Last-Event-ID", it)
            }
        }.execute { response ->
            if (response.status != HttpStatusCode.OK) throw response.toClusterException()

            val channel = response.bodyAsChannel()
            val dataLines = StringBuilder()
            suspend fun dispatch() {
                if (dataLines.isEmpty()) return
                val payload = dataLines.toString()
                dataLines.clear()
                val event = runCatching {
                    json.decodeFromString(TaskEvent.serializer(), payload)
                }.getOrNull() ?: return
                onEvent(event)
            }

            while (true) {
                val line = channel.readUTF8Line() ?: break
                when {
                    line.isEmpty() -> dispatch()
                    line.startsWith("data:") -> {
                        if (dataLines.isNotEmpty()) dataLines.append('\n')
                        dataLines.append(line.removePrefix("data:").trimStart())
                    }
                    // id/event 行只用于 SSE 协议；任务游标以事件体里的 event_id 为准。
                }
            }
            dispatch()
        }
    }

    override suspend fun getResult(
        endpoint: ZhentuEndpoint,
        resultUrl: String,
    ): ResultEnvelope {
        return http.prepareGet(resolveUrl(endpoint.baseUrl, resultUrl)) {
            applyAuth(endpoint)
            accept(ContentType.Application.Json)
        }.execute { response ->
            if (response.status != HttpStatusCode.OK) throw response.toClusterException()
            val text = response.bodyAsText()
            val status = runCatching {
                json.parseToJsonElement(text).jsonObject["status"]?.jsonPrimitive?.contentOrNull
            }.getOrNull()
            if (status != null && status !in TERMINAL_STATUSES) {
                throw ClusterException(0, "task_not_finished", "任务尚未结束（$status）")
            }
            runCatching { json.decodeFromString(ResultEnvelope.serializer(), text) }
                .getOrElse { throw ClusterException(0, "invalid_response", "阵图结果解析失败：${it.message}") }
        }
    }

    override suspend fun cancel(endpoint: ZhentuEndpoint, taskId: String) {
        http.preparePost("${apiBase(endpoint)}/requests/$taskId/cancel") {
            applyAuth(endpoint)
        }.execute { response ->
            if (response.status != HttpStatusCode.OK) throw response.toClusterException()
        }
    }

    override fun close() = http.close()

    private fun io.ktor.client.request.HttpRequestBuilder.applyAuth(endpoint: ZhentuEndpoint) {
        endpoint.token?.takeIf { it.isNotBlank() }?.let {
            header(HttpHeaders.Authorization, "Bearer $it")
        }
        endpoint.nodeId.takeIf { it.isNotBlank() }?.let {
            header("X-Node-Id", it)
        }
    }

    private suspend fun HttpResponse.toClusterException(): ClusterException {
        val text = runCatching { bodyAsText() }.getOrDefault("")
        val error = runCatching {
            json.parseToJsonElement(text).jsonObject["error"]?.jsonObject
        }.getOrNull()
        val message = error?.get("message")?.jsonPrimitive?.contentOrNull
            ?: "阵图返回 HTTP ${status.value}"
        val code = error?.get("code")?.jsonPrimitive?.contentOrNull
            ?: if (status.value == 410) "replay_unavailable" else null
        return ClusterException(status.value, code, message)
    }

    private fun apiBase(endpoint: ZhentuEndpoint): String {
        val base = endpoint.baseUrl.trim().trimEnd('/')
        return if (base.endsWith("/zhentu/v1")) base else "$base/zhentu/v1"
    }

    /** 相对路径按阵图根地址补全（events_url/result_url 返回的是 `/zhentu/v1/...`）。 */
    private fun resolveUrl(baseUrl: String, path: String): String {
        if (path.startsWith("http://") || path.startsWith("https://")) return path
        return "${originOf(baseUrl)}/${path.trimStart('/')}"
    }

    private fun originOf(baseUrl: String): String {
        val trimmed = baseUrl.trim().trimEnd('/')
        val schemeEnd = trimmed.indexOf("://")
        if (schemeEnd < 0) return trimmed
        val pathStart = trimmed.indexOf('/', schemeEnd + 3)
        return if (pathStart < 0) trimmed else trimmed.substring(0, pathStart)
    }

    private companion object {
        val TERMINAL_STATUSES = setOf("completed", "partial", "failed", "canceled")
    }
}
