package com.biyou.sanxian.node

import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.LongAdder

/**
 * 进程内 Prometheus 指标（与准圣 `quasheng_*`、阵图 `zhentu_*` 同风格，前缀 `sanxian_`）。
 *
 * **定位**：只服务 `/metrics` 这个运维端点，**不属于 `/v1` 下的契约面**——不新增入站契约端点、
 * 不改响应头、不改任何既有响应形状。
 *
 * **基数约束**：标签值只能来自有限集合（配置里的模型白名单、能力枚举、状态码），
 * 绝不把请求体里的任意字符串直接当标签（否则 Prometheus 侧时间序列爆炸）。
 * 调用方负责把未知模型归一成 `unknown`（见 [NodeApi.modelLabel]）。
 *
 * 线程安全：计数器用 [LongAdder]，直方图用 `synchronized` 保护（桶数量固定，竞争很低）。
 */
class NodeMetrics {

    private val counters = ConcurrentHashMap<Key, LongAdder>()
    private val histograms = ConcurrentHashMap<Key, Histogram>()

    // ----------------------------- 写入侧 -----------------------------

    /** 记录一次 HTTP 请求（端点级）。[endpoint] 必须是归一化路径，不是原始 URI。 */
    fun observeHttp(endpoint: String, method: String, status: Int) {
        counter("sanxian_http_requests_total", "endpoint" to endpoint, "method" to method, "status" to status.toString())
            .increment()
    }

    /**
     * 记录一次模型请求。[capability] ∈ chat / vision / embedding；[status] 为 `ok`、
     * HTTP 状态码字符串或 `cancelled`（客户端断连）。
     */
    fun observeModel(
        model: String,
        capability: String,
        status: String,
        durationSeconds: Double,
        firstByteSeconds: Double?,
    ) {
        counter(
            "sanxian_model_requests_total",
            "model" to model, "capability" to capability, "status" to status,
        ).increment()
        val labels = listOf("model" to model, "capability" to capability)
        histogram("sanxian_request_duration_seconds", labels).observe(durationSeconds)
        // 非流式没有「首字节」的独立语义，首字节即整体耗时（与准圣一致）
        histogram("sanxian_first_byte_seconds", labels).observe(firstByteSeconds ?: durationSeconds)
    }

    // ----------------------------- 渲染侧 -----------------------------

    /**
     * 渲染 Prometheus 文本格式。[gauges] 由调用方在 scrape 时提供（并发数、温度、模型加载态等），
     * 这样指标始终反映抓取瞬间的真实状态，而不是上次请求时的快照。
     */
    fun render(gauges: List<Gauge>): String {
        val out = StringBuilder(4096)
        appendCounters(out)
        appendHistograms(out)
        appendGauges(out, gauges)
        return out.toString()
    }

    /** 即时值指标：抓取时计算，不在请求路径上累加。 */
    data class Gauge(
        val name: String,
        val help: String,
        val labels: List<Pair<String, String>> = emptyList(),
        val value: Double,
    )

    /** 抓取用的直方图桶边界（秒）。与准圣 `metrics.py` 的 13 档保持一致，便于跨节点对比。 */
    private companion object {
        val BUCKETS = doubleArrayOf(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

        /** 已注册指标的 HELP 文案；未登记的名字退化为指标名本身。 */
        val HELP = mapOf(
            "sanxian_http_requests_total" to "HTTP 请求总数（按端点/方法/状态码）。",
            "sanxian_model_requests_total" to "模型请求总数（按模型/能力/结果）。",
            "sanxian_request_duration_seconds" to "模型请求整体耗时。",
            "sanxian_first_byte_seconds" to "模型请求首字节耗时（非流式等于整体耗时）。",
            "sanxian_inflight_requests" to "当前在途请求数（并发闸门占用）。",
            "sanxian_queue_len" to "真实排队数；满载立即 429，故恒为 0。",
            "sanxian_effective_max_concurrency" to "当前生效并发上限（降载时为 1）。",
            "sanxian_thermal_shedding" to "是否因发热降载（1=是）。",
            "sanxian_temperature_celsius" to "电池温度（摄氏度）。",
            "sanxian_battery_percent" to "电量百分比。",
            "sanxian_mem_used_mb" to "已用内存（MB）。",
            "sanxian_model_loaded" to "模型是否已加载（1=是）。",
            "sanxian_up" to "节点进程存活并成功抓取（1=是）。",
        )
    }

    private data class Key(val name: String, val labels: String)

    private class Histogram {
        val counts = LongArray(BUCKETS.size)
        var sum = 0.0
        var total = 0L

        fun observe(seconds: Double) = synchronized(this) {
            val value = if (seconds.isNaN() || seconds < 0.0) 0.0 else seconds
            sum += value
            total += 1
            for (i in BUCKETS.indices) {
                if (value <= BUCKETS[i]) counts[i] += 1
            }
        }

        fun snapshot(): Triple<LongArray, Double, Long> = synchronized(this) {
            Triple(counts.copyOf(), sum, total)
        }
    }

    private fun counter(name: String, vararg labels: Pair<String, String>): LongAdder =
        counters.getOrPut(Key(name, labelText(labels.toList()))) { LongAdder() }

    private fun histogram(name: String, labels: List<Pair<String, String>>): Histogram =
        histograms.getOrPut(Key(name, labelText(labels))) { Histogram() }

    private fun appendCounters(out: StringBuilder) {
        val grouped = counters.entries
            .groupBy({ it.key.name }, { it.key.labels to it.value.sum() })
            .toSortedMap()
        for ((name, samples) in grouped) {
            out.append("# HELP ").append(name).append(' ').append(HELP[name] ?: name).append('\n')
            out.append("# TYPE ").append(name).append(" counter\n")
            for ((labels, value) in samples.sortedBy { it.first }) {
                out.append(name).append(labels).append(' ').append(value).append('\n')
            }
        }
    }

    private fun appendHistograms(out: StringBuilder) {
        val grouped = histograms.entries
            .groupBy({ it.key.name }, { it.key.labels to it.value.snapshot() })
            .toSortedMap()
        for ((name, samples) in grouped) {
            out.append("# HELP ").append(name).append(' ').append(HELP[name] ?: name).append('\n')
            out.append("# TYPE ").append(name).append(" histogram\n")
            for ((labels, snapshot) in samples.sortedBy { it.first }) {
                val (counts, sum, total) = snapshot
                // counts 在 observe 时已按「value <= bound」累计过，这里直接输出，避免二次累加
                for (i in BUCKETS.indices) {
                    out.append(name).append("_bucket")
                        .append(withLabel(labels, "le", formatBound(BUCKETS[i])))
                        .append(' ').append(counts[i]).append('\n')
                }
                out.append(name).append("_bucket").append(withLabel(labels, "le", "+Inf"))
                    .append(' ').append(total).append('\n')
                out.append(name).append("_sum").append(labels).append(' ').append(sum).append('\n')
                out.append(name).append("_count").append(labels).append(' ').append(total).append('\n')
            }
        }
    }

    /**
     * 同名 gauge 只输出一次 HELP/TYPE（Prometheus 对重复 HELP 会直接解析失败），
     * 多标签的样本连续输出。样本按标签文本排序，保证抓取结果稳定可比。
     */
    private fun appendGauges(out: StringBuilder, gauges: List<Gauge>) {
        val grouped = gauges.groupBy { it.name }.toSortedMap()
        for ((name, samples) in grouped) {
            out.append("# HELP ").append(name).append(' ').append(samples.first().help).append('\n')
            out.append("# TYPE ").append(name).append(" gauge\n")
            for (gauge in samples.sortedBy { labelText(it.labels) }) {
                out.append(name).append(labelText(gauge.labels)).append(' ')
                    .append(formatValue(gauge.value)).append('\n')
            }
        }
    }

    private fun labelText(labels: List<Pair<String, String>>): String =
        if (labels.isEmpty()) "" else labels.joinToString(",", "{", "}") { (k, v) -> "$k=\"${escape(v)}\"" }

    private fun withLabel(labels: String, key: String, value: String): String {
        val extra = "$key=\"${escape(value)}\""
        return if (labels.isEmpty()) "{$extra}" else labels.dropLast(1) + "," + extra + "}"
    }

    private fun escape(raw: String): String = raw
        .replace("\\", "\\\\")
        .replace("\"", "\\\"")
        .replace("\n", "\\n")

    private fun formatBound(bound: Double): String =
        if (bound == bound.toLong().toDouble()) bound.toLong().toString() else bound.toString()

    private fun formatValue(value: Double): String = when {
        value.isNaN() -> "NaN"
        value.isInfinite() -> if (value > 0) "+Inf" else "-Inf"
        value == value.toLong().toDouble() -> value.toLong().toString()
        else -> value.toString()
    }
}

/**
 * 单次模型请求的计时器：开始计时 → 记录首字节 → 结束时按结果落指标。
 *
 * 首字节语义：流式为首个 content 分片写入时刻；非流式不调用 [firstByte]，
 * 结束时以整体耗时作为首字节耗时（与准圣口径一致）。
 */
class ModelObservation(
    private val metrics: NodeMetrics,
    private val model: String,
    private val capability: String,
) {
    private val startedNs = System.nanoTime()
    private var firstByteNs: Long? = null

    fun firstByte() {
        if (firstByteNs == null) firstByteNs = System.nanoTime()
    }

    fun finish(status: String) {
        val nowNs = System.nanoTime()
        metrics.observeModel(
            model = model,
            capability = capability,
            status = status,
            durationSeconds = (nowNs - startedNs) / 1e9,
            firstByteSeconds = firstByteNs?.let { (it - startedNs) / 1e9 },
        )
    }

    /** 异常 → 指标状态：节点异常取真实 HTTP 状态，客户端断连单列，其余按 500。 */
    fun finishWith(error: Throwable?) {
        finish(
            when (error) {
                null -> "ok"
                is NodeException -> error.status.toString()
                // kotlinx 的 CancellationException 在 JVM 上就是 java.util.concurrent 的同名类型
                is java.util.concurrent.CancellationException -> "cancelled"
                else -> "500"
            }
        )
    }
}
