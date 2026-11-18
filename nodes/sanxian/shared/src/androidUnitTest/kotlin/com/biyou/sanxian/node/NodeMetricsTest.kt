package com.biyou.sanxian.node

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * [NodeMetrics] 的纯渲染测试：不启动 Ktor，只验证文本格式与累加语义。
 * 端点级行为由 `NodeServerIntegrationTest` 覆盖。
 */
class NodeMetricsTest {

    private fun NodeMetrics.text(vararg gauges: NodeMetrics.Gauge) = render(gauges.toList())

    @Test
    fun `counter 按标签累计并渲染 HELP TYPE`() {
        val m = NodeMetrics()
        m.observeHttp("/v1/health", "GET", 200)
        m.observeHttp("/v1/health", "GET", 200)
        m.observeHttp("/v1/chat/completions", "POST", 429)

        val text = m.text()
        assertTrue(text.contains("# TYPE sanxian_http_requests_total counter"), text)
        assertTrue(
            text.contains("""sanxian_http_requests_total{endpoint="/v1/health",method="GET",status="200"} 2"""),
            text,
        )
        assertTrue(
            text.contains("""sanxian_http_requests_total{endpoint="/v1/chat/completions",method="POST",status="429"} 1"""),
            text,
        )
    }

    @Test
    fun `histogram 累积分桶且首字节缺省等于整体耗时`() {
        val m = NodeMetrics()
        m.observeModel("qwen.gguf", "chat", "ok", durationSeconds = 0.3, firstByteSeconds = null)
        m.observeModel("qwen.gguf", "chat", "ok", durationSeconds = 2.0, firstByteSeconds = 0.5)

        val text = m.text()
        assertTrue(
            text.contains("""sanxian_request_duration_seconds_count{model="qwen.gguf",capability="chat"} 2"""),
            text,
        )
        // 0.3s 落进 0.5 桶，2.0s 落进 2.5 桶；桶是累计值
        assertTrue(
            text.contains(
                """sanxian_request_duration_seconds_bucket{model="qwen.gguf",capability="chat",le="0.5"} 1"""
            ),
            text,
        )
        assertTrue(
            text.contains(
                """sanxian_request_duration_seconds_bucket{model="qwen.gguf",capability="chat",le="2.5"} 2"""
            ),
            text,
        )
        assertTrue(
            text.contains(
                """sanxian_request_duration_seconds_bucket{model="qwen.gguf",capability="chat",le="+Inf"} 2"""
            ),
            text,
        )
        // 第一次没有首字节 → 用整体耗时 0.3 计入 0.5 桶
        assertTrue(
            text.contains("""sanxian_first_byte_seconds_bucket{model="qwen.gguf",capability="chat",le="0.5"} 2"""),
            text,
        )
        assertTrue(
            text.contains("""sanxian_first_byte_seconds_count{model="qwen.gguf",capability="chat"} 2"""),
            text,
        )
    }

    @Test
    fun `gauge 渲染即时值并把整数显示为整数`() {
        val m = NodeMetrics()
        val text = m.text(
            NodeMetrics.Gauge("sanxian_up", "存活", value = 1.0),
            NodeMetrics.Gauge("sanxian_model_loaded", "加载态", labels = listOf("model" to "a.gguf"), value = 0.0),
            NodeMetrics.Gauge("sanxian_temperature_celsius", "温度", value = 36.5),
        )
        assertTrue(text.contains("# TYPE sanxian_up gauge"), text)
        assertTrue(text.contains("sanxian_up 1\n"), text)
        assertTrue(text.contains("""sanxian_model_loaded{model="a.gguf"} 0"""), text)
        // 同名 gauge 多标签时 HELP/TYPE 只能出现一次，否则 Prometheus 解析会报错
        val multi = NodeMetrics().text(
            NodeMetrics.Gauge("sanxian_model_loaded", "加载态", labels = listOf("model" to "a.gguf"), value = 1.0),
            NodeMetrics.Gauge("sanxian_model_loaded", "加载态", labels = listOf("model" to "b.gguf"), value = 0.0),
        )
        assertEquals(1, multi.lines().count { it.startsWith("# HELP sanxian_model_loaded") })
        assertEquals(1, multi.lines().count { it.startsWith("# TYPE sanxian_model_loaded") })
        assertEquals(2, multi.lines().count { it.startsWith("sanxian_model_loaded{") })
        assertTrue(text.contains("sanxian_temperature_celsius 36.5"), text)
    }

    @Test
    fun `标签值转义引号与换行`() {
        val m = NodeMetrics()
        m.observeHttp("/v1/chat/completions", "POST", 500)
        // 直接构造含特殊字符的 gauge，验证转义而不是让文本格式崩掉
        val text = m.text(
            NodeMetrics.Gauge("sanxian_custom", "自定义", labels = listOf("v" to "a\"b\nc\\d"), value = 1.0)
        )
        assertTrue(text.contains("""sanxian_custom{v="a\"b\nc\\d"} 1"""), text)
    }

    @Test
    fun `模型观察器按异常类型归类状态`() {
        val m = NodeMetrics()
        ModelObservation(m, "qwen.gguf", "chat").finishWith(null)
        ModelObservation(m, "qwen.gguf", "chat").finishWith(badRequest("坏请求"))
        ModelObservation(m, "qwen.gguf", "vision").finishWith(java.util.concurrent.CancellationException("断开"))
        ModelObservation(m, "qwen.gguf", "chat").finishWith(IllegalStateException("其他"))

        val text = m.text()
        assertTrue(text.contains("""capability="chat",status="ok"} 1"""), text)
        assertTrue(text.contains("""capability="chat",status="400"} 1"""), text)
        assertTrue(text.contains("""capability="chat",status="500"} 1"""), text)
        assertTrue(text.contains("""capability="vision",status="cancelled"} 1"""), text)
        assertEquals(4, countModelSamples(text))
    }

    private fun countModelSamples(text: String): Int =
        text.lines().count { it.startsWith("sanxian_model_requests_total{") }
}
