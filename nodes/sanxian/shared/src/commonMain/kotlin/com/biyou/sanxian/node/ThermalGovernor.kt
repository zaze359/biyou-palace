package com.biyou.sanxian.node

/**
 * 设备热信号快照。[statusLevel] 为 PowerManager 热状态等级（0=none … 6=shutdown），
 * [tempC] 为电池温度（℃）。两者为 null 表示该信号不可用（很多设备不提供）——
 * **不可用绝不能被当成过热**。
 */
data class ThermalSignal(
    val statusLevel: Int? = null,
    val tempC: Double? = null,
)

/**
 * 发热降载闸门。
 *
 * 触发：热状态等级 >= `thermal_shed_status` 或温度 >= `thermal_shed_c`。
 * 降载期间：有效并发压到 1；**新请求立即 503**（`thermal_shedding` + Retry-After），
 * 已在途请求不受影响（本类不持有请求，也不主动中断）。
 * 恢复：温度需回落到 `thermal_resume_c` 以下（迟滞），避免阈值附近反复抖动。
 * 两个信号都缺失时保持正常服务，不降载。
 */
class ThermalGovernor(private val config: NodeConfig) {

    private val lock = Any()
    private var shedding = false
    private var reason: String? = null

    val isShedding: Boolean get() = synchronized(lock) { shedding }

    val shedReason: String? get() = synchronized(lock) { reason }

    /** 降载期间 1，否则配置的并发上限。 */
    val effectiveLimit: Int get() = if (isShedding) 1 else config.maxConcurrency

    /** 用最新热信号刷新状态；返回刷新后是否降载。 */
    fun update(signal: ThermalSignal): Boolean = synchronized(lock) {
        val decision = ThermalShed.decide(signal, config, shedding)
        shedding = decision.shedding
        reason = decision.reason
        shedding
    }

    /** 准入检查：降载期间拒绝新请求（503），在途请求由调用方自行跑完。 */
    fun admit() {
        val current = synchronized(lock) { shedding to reason }
        if (current.first) {
            throw thermalShedding(
                message = "设备温度过高，节点已降载（${current.second ?: "thermal"}），请稍后重试",
                retryAfterSeconds = config.thermalRetryAfterS,
            )
        }
    }
}

/** 纯判定逻辑（便于单测）：无状态，由 [ThermalGovernor] 传入当前是否已降载。 */
object ThermalShed {

    data class Decision(val shedding: Boolean, val reason: String?)

    fun decide(signal: ThermalSignal, config: NodeConfig, currentlyShedding: Boolean): Decision {
        val statusHot = config.thermalShedStatus > 0 &&
            signal.statusLevel != null && signal.statusLevel >= config.thermalShedStatus
        val tempHot = signal.tempC != null && signal.tempC >= config.thermalShedC

        if (!currentlyShedding) {
            return when {
                statusHot && tempHot -> Decision(true, "thermal_status=${signal.statusLevel}, temperature=${signal.tempC}℃")
                statusHot -> Decision(true, "thermal_status=${signal.statusLevel}")
                tempHot -> Decision(true, "temperature=${signal.tempC}℃ ≥ ${config.thermalShedC}℃")
                else -> Decision(false, null)
            }
        }

        // 已降载：恢复需要同时满足热状态回落与温度低于恢复阈值（迟滞）
        val resumeC = resumeThreshold(config)
        val statusCool = config.thermalShedStatus <= 0 ||
            signal.statusLevel == null || signal.statusLevel < config.thermalShedStatus
        val tempCool = signal.tempC == null || signal.tempC <= resumeC
        if (statusCool && tempCool) return Decision(false, null)

        val stillHot = buildString {
            if (!statusCool) append("thermal_status=${signal.statusLevel}")
            if (!tempCool) {
                if (isNotEmpty()) append(", ")
                append("temperature=${signal.tempC}℃ > ${resumeC}℃")
            }
        }
        return Decision(true, stillHot)
    }

    /** 恢复阈值必须低于触发阈值；配置写反时自动收敛 0.1℃，避免抖动。 */
    private fun resumeThreshold(config: NodeConfig): Double =
        minOf(config.thermalResumeC, config.thermalShedC - 0.1)
}
