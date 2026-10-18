package com.biyou.sanxian.node

/**
 * OpenAI 风格 SSE 块拼装。
 *
 * 与准圣（Python）冒烟验证的输出**逐字节对齐**：首块 `delta.role`、中间块
 * `delta.content`、末块 `delta:{}` + `finish_reason:"stop"`、`data: [DONE]` 收尾。
 *
 * 采用手工拼装而非序列化，是为了精确控制 `delta:{}`（无 content 字段）与
 * `finish_reason:null` 的呈现——这是契约要求的流式终止形态，且客户端对
 * `delta.content=null` 必须有保护（不渲染空串）。
 */
object Sse {
    const val EVENT_SUFFIX = "\n\n"

    private fun esc(s: String): String =
        s.replace("\\", "\\\\")
            .replace("\"", "\\\"")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")

    /** 首块：声明 assistant 角色，content 不出现 */
    fun roleBlock(id: String, model: String): String =
        """data: {"id":"$id","object":"chat.completion.chunk","model":"$model","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}"""

    /** 中间块：增量文本 */
    fun contentBlock(id: String, model: String, content: String): String =
        """data: {"id":"$id","object":"chat.completion.chunk","model":"$model","choices":[{"index":0,"delta":{"content":"${esc(content)}"},"finish_reason":null}]}"""

    /** 末块：delta 为空对象（content=null），finish_reason=stop */
    fun stopBlock(id: String, model: String): String =
        """data: {"id":"$id","object":"chat.completion.chunk","model":"$model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}"""

    const val DONE = "data: [DONE]"

    fun wrap(line: String): String = "$line$EVENT_SUFFIX"

    /** 把一行 SSE 块 + 终止符拼成可直接写入响应的字符串 */
    fun emit(line: String): String = wrap(line)
}
