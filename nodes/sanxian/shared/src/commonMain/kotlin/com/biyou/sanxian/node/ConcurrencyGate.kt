package com.biyou.sanxian.node

import java.util.concurrent.atomic.AtomicInteger

/**
 * 并发闸门（与准圣 [ConcurrencyGate] 同构）。
 *
 * 语义：**满载立即 429**，不排队等待。
 * - [acquire]：若已达上限，抛 [NodeException](429)；否则占用一个名额返回。
 * - [release]：释放名额（必须在请求结束时调用，含异常/断连路径）。
 * - [active]：当前占用数，供 `/v1/health` 的 `queue_len` 上报。
 *
 * 关键坑（准圣已踩并修复）：**流式响应不能用「进入即释放」的上下文管理器**——
 * 返回 `StreamingResponse` 时管理器就退出了，名额被提前释放，429 永不被触发。
 * 流式必须显式 `acquire()` → 在流结束的 `finally` 中 `release()`。本类只提供
 * 原子 acquire/release，由调用方按此规则使用。
 */
class ConcurrencyGate(val limit: Int) {
    init {
        require(limit >= 1) { "max_concurrency 必须 ≥ 1" }
    }

    private val current = AtomicInteger(0)

    val active: Int get() = current.get()

    fun acquire() {
        while (true) {
            val n = current.get()
            if (n >= limit) {
                throw concurrencyLimitExceeded("并发已满（上限 $limit），请稍后重试")
            }
            if (current.compareAndSet(n, n + 1)) return
        }
    }

    fun release() {
        current.decrementAndGet()
    }
}
