package com.biyou.sanxian.node

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * SSE 块拼装：与契约逐字节对齐（首块 role、中间块 content、末块 delta:{}、[DONE] 收尾）。
 * 这些字符串直接发给客户端，改动需要同步三件套客户端，因此用精确断言锁住。
 */
class SseTest {

    private val id = "chatcmpl-sanxian-1"
    private val model = "Qwen3-0.6B-Q8_0.gguf"

    @Test
    fun `首块声明 assistant 角色且不含 content`() {
        assertEquals(
            """data: {"id":"$id","object":"chat.completion.chunk","model":"$model","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}""",
            Sse.roleBlock(id, model),
        )
    }

    @Test
    fun `中间块只含增量文本`() {
        assertEquals(
            """data: {"id":"$id","object":"chat.completion.chunk","model":"$model","choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":null}]}""",
            Sse.contentBlock(id, model, "你好"),
        )
    }

    @Test
    fun `末块 delta 为空对象并带 stop`() {
        val block = Sse.stopBlock(id, model)
        assertTrue(block.contains("\"delta\":{}"))
        assertTrue(block.contains("\"finish_reason\":\"stop\""))
    }

    @Test
    fun `内容转义保持 JSON 合法`() {
        val block = Sse.contentBlock(id, model, "a\"b\\c\nd\te\r")
        assertTrue(block.contains("""a\"b\\c\nd\te\r"""))
    }

    @Test
    fun `emit 以双换行结尾且 DONE 固定`() {
        assertEquals("data: [DONE]\n\n", Sse.emit(Sse.DONE))
        assertEquals(
            """data: {"x":1}""" + "\n\n",
            Sse.wrap("""data: {"x":1}"""),
        )
    }
}
