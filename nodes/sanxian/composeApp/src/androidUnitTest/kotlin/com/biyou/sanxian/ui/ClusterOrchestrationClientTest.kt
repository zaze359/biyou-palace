package com.biyou.sanxian.ui

import com.biyou.sanxian.llm.ChatMessage
import io.ktor.client.HttpClient
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpMethod
import io.ktor.http.HttpStatusCode
import io.ktor.http.content.TextContent
import io.ktor.http.headersOf
import kotlinx.coroutines.runBlocking
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

/**
 * 阵图客户端：路径、鉴权、幂等、SSE 解析、410 与取消。
 */
class ClusterOrchestrationClientTest {

    private val endpoint = ZhentuEndpoint("http://192.168.1.10:8100", "zt-token", "sanxian-01")

    private fun envelope() = ClusterRequestEnvelopeBuilder.build(
        originNodeId = "sanxian-01",
        requestedMode = ChatMode.AUTO,
        messages = listOf(ChatMessage.user("你好")),
    )

    @Test
    fun `submit 发往 requests 并解析 task_id 与幂等标记`() = runBlocking {
        var capturedPath = ""
        var capturedMethod: HttpMethod? = null
        var capturedAuth: String? = null
        var capturedNode: String? = null
        var capturedBody = ""
        val engine = MockEngine { request ->
            capturedPath = request.url.encodedPath
            capturedMethod = request.method
            capturedAuth = request.headers[HttpHeaders.Authorization]
            capturedNode = request.headers["X-Node-Id"]
            capturedBody = (request.body as TextContent).text
            respond(
                content = """{"task_id":"task-1","events_url":"/zhentu/v1/requests/task-1/events",
                    "result_url":"/zhentu/v1/requests/task-1","duplicate":true}""".trimIndent(),
                status = HttpStatusCode.OK,
                headers = headersOf(HttpHeaders.ContentType, "application/json"),
            )
        }
        val client = KtorClusterOrchestrationClient(HttpClient(engine))

        val submit = client.submit(endpoint, envelope())
        client.close()

        assertEquals("/zhentu/v1/requests", capturedPath)
        assertEquals(HttpMethod.Post, capturedMethod)
        assertEquals("Bearer zt-token", capturedAuth)
        assertEquals("sanxian-01", capturedNode)
        assertTrue(capturedBody.contains("\"mode\":\"cluster\""))
        assertTrue(capturedBody.contains("\"requested_mode\":\"auto\""))
        assertEquals("task-1", submit.taskId)
        assertTrue(submit.duplicate)
    }

    @Test
    fun `streamEvents 解析 SSE 并携带 Last-Event-ID`() = runBlocking {
        var lastEventHeader: String? = null
        val engine = MockEngine { request ->
            lastEventHeader = request.headers["Last-Event-ID"]
            respond(
                content = """
                    id: 1
                    event: step/started
                    data: {"event_id":"evt-1","task_id":"task-1","seq":1,"type":"step/started","at":"2026-11-21T00:00:00+08:00","data":{"step_id":"s1"}}

                    id: 2
                    event: task/completed
                    data: {"event_id":"evt-2","task_id":"task-1","seq":2,"type":"task/completed","at":"2026-11-21T00:00:01+08:00","data":{}}

                """.trimIndent() + "\n\n",
                status = HttpStatusCode.OK,
                headers = headersOf(HttpHeaders.ContentType, "text/event-stream"),
            )
        }
        val client = KtorClusterOrchestrationClient(HttpClient(engine))

        val events = mutableListOf<String>()
        client.streamEvents(endpoint, "/zhentu/v1/requests/task-1/events", "evt-0") {
            events += it.type
        }
        client.close()

        assertEquals("evt-0", lastEventHeader)
        assertEquals(listOf("step/started", "task/completed"), events)
    }

    @Test
    fun `streamEvents 在 410 时抛出可识别的 replay_unavailable`() = runBlocking {
        val engine = MockEngine {
            respond(
                content = """{"error":{"code":"replay_unavailable","message":"事件已过期","type":"orchestration_error"}}""",
                status = HttpStatusCode.Gone,
                headers = headersOf(HttpHeaders.ContentType, "application/json"),
            )
        }
        val client = KtorClusterOrchestrationClient(HttpClient(engine))

        val error = assertFailsWith<ClusterException> {
            client.streamEvents(endpoint, "/zhentu/v1/requests/task-1/events", "evt-old") {}
        }
        client.close()

        assertEquals(410, error.status)
        assertEquals("replay_unavailable", error.code)
    }

    @Test
    fun `getResult 解析终态结果`() = runBlocking {
        val engine = MockEngine {
            respond(
                content = """
                    {"schema_version":"1.0","task_id":"task-1","status":"completed",
                    "terminal_reason":null,"mode":"cluster","answer":"聚合答案",
                    "sources":[{"step_id":"s1","node_id":"quasheng-01","model":"qwen"}],
                    "partials":[],"usage":{"prompt_tokens":3,"completion_tokens":5,"steps":1},
                    "error":null}
                """.trimIndent(),
                status = HttpStatusCode.OK,
                headers = headersOf(HttpHeaders.ContentType, "application/json"),
            )
        }
        val client = KtorClusterOrchestrationClient(HttpClient(engine))

        val result = client.getResult(endpoint, "/zhentu/v1/requests/task-1")
        client.close()

        assertEquals("completed", result.status)
        assertEquals("聚合答案", result.answer)
        assertEquals("s1", result.sources.single().stepId)
    }

    @Test
    fun `cancel 调用任务取消端点`() = runBlocking {
        var capturedPath = ""
        var capturedMethod: HttpMethod? = null
        val engine = MockEngine { request ->
            capturedPath = request.url.encodedPath
            capturedMethod = request.method
            respond(
                content = """{"task_id":"task-1","status":"canceled"}""",
                status = HttpStatusCode.OK,
                headers = headersOf(HttpHeaders.ContentType, "application/json"),
            )
        }
        val client = KtorClusterOrchestrationClient(HttpClient(engine))

        client.cancel(endpoint, "task-1")
        client.close()

        assertEquals("/zhentu/v1/requests/task-1/cancel", capturedPath)
        assertEquals(HttpMethod.Post, capturedMethod)
    }
}
