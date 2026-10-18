package com.biyou.sanxian.node

/**
 * 节点状态判定（纯函数，便于单测；与准圣 [determine_status] 同源逻辑）。
 * 优先级：down > loading > degraded > ready。
 *
 * - down      ：引擎不可用（库未链接 / 引擎初始化失败）→ 拒绝服务
 * - loading   ：预加载白名单模型尚未全部常驻 → 上层暂勿路由
 * - degraded  ：内存 / CPU / 温度越阈值 → 上层应降权
 * - ready     ：可服务
 */
object NodeStatus {
    const val DOWN = "down"
    const val LOADING = "loading"
    const val DEGRADED = "degraded"
    const val READY = "ready"

    fun determine(
        engineOk: Boolean,
        preloadLoaded: Boolean,
        memPct: Double?,
        cpuPct: Double?,
        tempC: Double?,
        config: NodeConfig,
    ): String {
        if (!engineOk) return DOWN
        if (!preloadLoaded) return LOADING
        val overheated = tempC != null && tempC >= config.tempWarnC
        val overMem = memPct != null && memPct >= config.memWarnPct
        val overCpu = cpuPct != null && cpuPct >= config.cpuWarnPct
        if (overheated || overMem || overCpu) return DEGRADED
        return READY
    }

    /** 预加载白名单是否全部已加载。空白名单视为已满足。 */
    fun preloadSatisfied(preloadModels: List<String>, loadedModels: Set<String>): Boolean =
        preloadModels.isEmpty() || preloadModels.all { it in loadedModels }
}
