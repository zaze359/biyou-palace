package com.biyou.sanxian.ui

import com.biyou.sanxian.contract.ChatMessage
import io.ktor.client.HttpClient
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.content.TextContent
import io.ktor.http.headersOf
import kotlinx.coroutines.runBlocking
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * 聊天客户端单测：验证请求确实发往节点 HTTP 端点、携带配置里的 token，
 * 且能正确解析节点返回的 SSE 增量帧。
 */
class NodeChatClientTest {

    @Test
    fun `streamChat 请求本机端点并解析 SSE 增量至 DONE`() = runBlocking {
        var capturedUrl = ""
        var capturedAuth: String? = null
        var capturedBody = ""
        val engine = MockEngine { request ->
            capturedUrl = request.url.toString()
            capturedAuth = request.headers[HttpHeaders.Authorization]
            capturedBody = (request.body as TextContent).text
            respond(
                content = """
                    data: {"choices":[{"delta":{"role":"assistant"}}]}

                    data: {"choices":[{"delta":{"content":"你"}}]}

                    data: {"choices":[{"delta":{"content":"好"}}]}

                    data: {"choices":[{"delta":{},"finish_reason":"stop"}]}

                    data: [DONE]

                """.trimIndent() + "\n\n",
                status = HttpStatusCode.OK,
                headers = headersOf(HttpHeaders.ContentType, "text/event-stream"),
            )
        }
        val client = KtorNodeChatClient(HttpClient(engine))

        val chunks = mutableListOf<String>()
        client.streamChat(
            endpoint = NodeChatEndpoint("http://127.0.0.1:4031/v1", "test-token"),
            model = "Qwen3-0.6B-Q8_0.gguf",
            messages = listOf(ChatMessage(role = "user", content = "你好")),
        ) { chunks += it }
        client.close()

        assertEquals("http://127.0.0.1:4031/v1/chat/completions", capturedUrl)
        assertEquals("Bearer test-token", capturedAuth)
        assertTrue(capturedBody.contains("\"stream\":true"))
        assertTrue(capturedBody.contains("Qwen3-0.6B-Q8_0.gguf"))
        assertEquals(listOf("你", "好"), chunks)
    }

    @Test
    fun `fetchModelStatus 只取已加载的 chat 模型`() = runBlocking {
        val engine = MockEngine {
            respond(
                content = """
                    {"node":{"id":"sanxian-01","name":"散仙","role":"sanxian","arch":"arm64",
                    "runtime":"llama.cpp","runtime_version":null,"contract_version":"1.0"},
                    "status":"ready",
                    "models":[
                      {"name":"Qwen3-0.6B-Q8_0.gguf","loaded":true,"capabilities":["chat"]},
                      {"name":"Qwen3-Embedding-0.6B-Q8_0.gguf","loaded":true,"capabilities":["embedding"]},
                      {"name":"other.gguf","loaded":false,"capabilities":["chat"]}
                    ],
                    "resources":{"cpu_pct":null,"mem_used_mb":null,"mem_total_mb":null,
                    "disk_free_mb":null,"queue_len":0},
                    "limits":{"max_concurrency":2,"max_context_len":null},
                    "updated_at":"2026-11-11T00:00:00+08:00"}
                """.trimIndent(),
                status = HttpStatusCode.OK,
                headers = headersOf(HttpHeaders.ContentType, "application/json"),
            )
        }
        val client = KtorNodeChatClient(HttpClient(engine))

        val status = client.fetchModelStatus(NodeChatEndpoint("http://127.0.0.1:4031/v1", null))
        client.close()

        assertEquals("Qwen3-0.6B-Q8_0.gguf", status.model)
        assertEquals("ready", status.nodeStatus)
        assertEquals(null, status.error)
    }

    @Test
    fun `fetchModelStatus 在节点无已加载 chat 模型时给出可读原因`() = runBlocking {
        val engine = MockEngine {
            respond(
                content = """
                    {"node":{"id":"sanxian-01","name":"散仙","role":"sanxian","arch":"arm64",
                    "runtime":"llama.cpp","runtime_version":null,"contract_version":"1.0"},
                    "status":"loading","models":[],
                    "resources":{"cpu_pct":null,"mem_used_mb":null,"mem_total_mb":null,
                    "disk_free_mb":null,"queue_len":0},
                    "limits":{"max_concurrency":2,"max_context_len":null},
                    "updated_at":"2026-11-11T00:00:00+08:00"}
                """.trimIndent(),
                status = HttpStatusCode.OK,
                headers = headersOf(HttpHeaders.ContentType, "application/json"),
            )
        }
        val client = KtorNodeChatClient(HttpClient(engine))

        val status = client.fetchModelStatus(NodeChatEndpoint("http://127.0.0.1:4031/v1", null))
        client.close()

        assertEquals(null, status.model)
        assertEquals("loading", status.nodeStatus)
        assertTrue(status.error!!.contains("chat 模型"))
    }
}
