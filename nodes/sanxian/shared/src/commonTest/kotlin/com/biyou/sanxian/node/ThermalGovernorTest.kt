package com.biyou.sanxian.node

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

/** 发热降载判定与迟滞恢复。 */
class ThermalGovernorTest {

    private val config = NodeConfig(maxConcurrency = 2)

    @Test
    fun `温度达到触发阈值即降载，有效并发压到 1`() {
        val governor = ThermalGovernor(config)

        assertTrue(governor.update(ThermalSignal(tempC = 48.0)))
        assertEquals(1, governor.effectiveLimit)
        assertTrue(governor.shedReason!!.contains("temperature"))

        val error = assertFailsWith<NodeException> { governor.admit() }
        assertEquals(503, error.status)
        assertEquals("thermal_shedding", error.code)
        assertEquals(10, error.retryAfterSeconds)
    }

    @Test
    fun `降频（热状态 severe 及以上）同样触发降载`() {
        val governor = ThermalGovernor(config)

        assertTrue(governor.update(ThermalSignal(statusLevel = 3, tempC = 38.0)))
        assertEquals(1, governor.effectiveLimit)
        assertTrue(governor.shedReason!!.contains("thermal_status=3"))
    }

    @Test
    fun `恢复带迟滞：夹在触发与恢复阈值之间仍保持降载`() {
        val governor = ThermalGovernor(config)

        assertTrue(governor.update(ThermalSignal(tempC = 50.0)))
        assertTrue(governor.update(ThermalSignal(tempC = 46.0)), "46℃ 仍高于恢复阈值 45℃，不得立即恢复")
        assertTrue(governor.isShedding)

        assertFalse(governor.update(ThermalSignal(tempC = 44.9)))
        assertEquals(2, governor.effectiveLimit)
        assertEquals(null, governor.shedReason)
    }

    @Test
    fun `热信号不可用时不降载也不崩溃`() {
        val governor = ThermalGovernor(config)

        assertFalse(governor.update(ThermalSignal()))
        assertEquals(2, governor.effectiveLimit)
        governor.admit() // 不抛异常
    }

    @Test
    fun `降载后信号重新不可用则恢复`() {
        val governor = ThermalGovernor(config)

        assertTrue(governor.update(ThermalSignal(statusLevel = 4)))
        assertFalse(governor.update(ThermalSignal()), "信号缺失不能被当成过热")
    }

    @Test
    fun `恢复阈值配置写反时自动收敛避免抖动`() {
        val bad = config.copy(thermalShedC = 48.0, thermalResumeC = 50.0)
        val governor = ThermalGovernor(bad)

        assertTrue(governor.update(ThermalSignal(tempC = 48.0)))
        assertTrue(governor.update(ThermalSignal(tempC = 48.0)), "恢复阈值收敛到 47.9，48.0 仍应保持降载")
        assertFalse(governor.update(ThermalSignal(tempC = 47.8)))
    }
}
