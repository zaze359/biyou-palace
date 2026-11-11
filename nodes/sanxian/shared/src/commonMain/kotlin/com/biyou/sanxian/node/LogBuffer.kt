package com.biyou.sanxian.node

import kotlinx.serialization.Serializable
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit

/** 单条运行事件（`/v1/logs` 返回元素）。 */
@Serializable
data class LogLine(
    val time: String,
    val level: String,
    val event: String,
    val message: String,
)

/** `GET /v1/logs` 响应，字段与准圣 [logbuffer.py] 对齐。 */
@Serializable
data class LogsResponse(
    val lines: List<LogLine>,
    val limit: Int,
    val capacity: Int,
    val total: Int,
)

/**
 * 运行时事件环形缓冲——`GET /v1/logs` 的唯一数据源，与准圣 `logbuffer.py` 同构。
 *
 * 只接受白名单事件（[EVENTS]），写入前统一脱敏：shared_token → `***`，
 * 常见绝对路径 → `<path>`，消息截断 500 字。本类不接触请求 body、请求头或 token 值。
 *
 * Kotlin 侧没有 Python logging handler，事件由既有日志点显式写入
 * （启动/预热在 NodeForegroundService，鉴权/并发/上游/断连/异常在 NodeApi）。
 */
object NodeLogBuffer {

    const val CAPACITY = 200

    /** 与准圣对齐的事件白名单；未登记的事件直接丢弃，避免缓冲被噪声占满。 */
    val EVENTS = setOf("startup", "warmup", "auth", "concurrency", "upstream", "disconnect", "exception")

    private val LEVELS = setOf("INFO", "WARNING", "ERROR")

    private val lock = Any()
    private val ring = ArrayDeque<LogLine>(CAPACITY)
    private var total = 0
    private var token: String? = null

    /**
     * 只匹配常见绝对路径前缀，避免把 URL 里的斜杠误伤。
     * 比准圣多覆盖 Android 应用目录（/data、/storage、/sdcard、/mnt），
     * 否则模型路径会在日志里原样暴露。
     */
    private val absPath = Regex(
        """(?:/Users|/home|/tmp|/private|/var|/opt|/root|/data|/storage|/sdcard|/mnt)/[^\s,，;)"']*"""
    )

    /** 登记需要脱敏的 token（节点启动时由 NodeApi/前台服务调用）。 */
    fun configure(sharedToken: String?) {
        synchronized(lock) {
            token = sharedToken?.takeIf { it.isNotBlank() }
        }
    }

    /** 写入一条白名单事件；level 非法按 INFO 处理，event 不在白名单则忽略。 */
    fun record(level: String, event: String, message: String) {
        if (event !in EVENTS) return
        val lvl = level.uppercase().takeIf { it in LEVELS } ?: "INFO"
        val time = OffsetDateTime.now().truncatedTo(ChronoUnit.SECONDS)
            .format(DateTimeFormatter.ISO_OFFSET_DATE_TIME)
        synchronized(lock) {
            val line = LogLine(
                time = time,
                level = lvl,
                event = event,
                message = scrub(message).take(500),
            )
            ring.addLast(line)
            while (ring.size > CAPACITY) ring.removeFirst()
            total++
        }
    }

    /** 最近 [limit] 条事件；limit 夹到 1..[CAPACITY]。 */
    fun recent(limit: Int = 50): LogsResponse = synchronized(lock) {
        val n = limit.coerceIn(1, CAPACITY)
        val lines = if (ring.size <= n) ring.toList() else ring.drop(ring.size - n)
        LogsResponse(lines = lines, limit = n, capacity = CAPACITY, total = total)
    }

    /** 清空缓冲（测试用）。 */
    fun clear() {
        synchronized(lock) {
            ring.clear()
            total = 0
            token = null
        }
    }

    private fun scrub(message: String): String {
        val text = token?.let { message.replace(it, "***") } ?: message
        return absPath.replace(text, "<path>")
    }
}
