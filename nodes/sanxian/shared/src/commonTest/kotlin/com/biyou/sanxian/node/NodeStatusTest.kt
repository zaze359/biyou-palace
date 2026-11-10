package com.biyou.sanxian.node

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/** NodeStatus 纯函数：状态优先级与资源阈值；采集失败（null）不得降级为崩溃。 */
class NodeStatusTest {

    private val config = NodeConfig(
        memWarnPct = 85.0,
        cpuWarnPct = 90.0,
        tempWarnC = 45.0,
    )

    @Test
    fun `健康且资源未知时为 ready`() {
        // 模拟原生采集全部失败：mem/cpu/temp 均为 null，应照常 ready 而不是崩溃或 degraded。
        assertEquals(
            NodeStatus.READY,
            NodeStatus.determine(
                engineOk = true,
                preloadLoaded = true,
                memPct = null,
                cpuPct = null,
                tempC = null,
                config = config,
            ),
        )
    }

    @Test
    fun `优先级 down 高于 loading 与 degraded`() {
        assertEquals(
            NodeStatus.DOWN,
            NodeStatus.determine(false, false, 99.0, 99.0, 99.0, config),
        )
        assertEquals(
            NodeStatus.LOADING,
            NodeStatus.determine(true, false, 99.0, 99.0, 99.0, config),
        )
    }

    @Test
    fun `任一资源越阈值即 degraded`() {
        assertEquals(NodeStatus.DEGRADED, NodeStatus.determine(true, true, 85.0, 0.0, 0.0, config))
        assertEquals(NodeStatus.DEGRADED, NodeStatus.determine(true, true, 0.0, 90.0, 0.0, config))
        assertEquals(NodeStatus.DEGRADED, NodeStatus.determine(true, true, 0.0, 0.0, 45.0, config))
    }

    @Test
    fun `阈值以下保持 ready`() {
        assertEquals(
            NodeStatus.READY,
            NodeStatus.determine(true, true, 84.9, 89.9, 44.9, config),
        )
    }

    @Test
    fun `预加载白名单判定`() {
        assertTrue(NodeStatus.preloadSatisfied(emptyList(), emptySet()))
        assertTrue(NodeStatus.preloadSatisfied(listOf("a.gguf"), setOf("a.gguf", "b.gguf")))
        assertFalse(NodeStatus.preloadSatisfied(listOf("a.gguf", "b.gguf"), setOf("a.gguf")))
    }
}
