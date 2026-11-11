package com.biyou.sanxian.node

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager
import android.os.StatFs
import com.biyou.sanxian.contract.Resources
import kotlinx.coroutines.delay
import java.io.File

/**
 * 散仙专属健康/资源采集（安卓）。与准圣的 `collect_resources` 对应，但**电量 / 温度 /
 * 热状态为真实值**（准圣恒为 null）。
 *
 * 任一采集失败一律降级为 `null`，绝不抛异常（资源采集失败不得导致 /v1/health 返回 500）。
 */
object NodeHealthCollector {

    data class CollectedHealth(
        val resources: Resources,
        val memPct: Double?,
        val cpuPct: Double?,
        val tempC: Double?,
        /** PowerManager 热状态等级（0=none … 6=shutdown）；平台不支持时为 null */
        val thermalStatusLevel: Int? = null,
    )

    suspend fun collect(context: Context, queueLen: Int): CollectedHealth {
        val batteryPct = readBatteryPct(context)
        val tempC = readTemperatureC(context)
        val statusLevel = readThermalStatusLevel(context)
        val thermalState = mapThermalStatus(statusLevel)
        val (memUsedMb, memTotalMb, memPct) = readMemory(context)
        val cpuPct = readCpuPct()
        val diskFreeMb = readDiskFreeMb(context)
        val resources = Resources(
            cpuPct = cpuPct,
            memUsedMb = memUsedMb,
            memTotalMb = memTotalMb,
            diskFreeMb = diskFreeMb,
            queueLen = queueLen,
            batteryPct = batteryPct,
            temperatureC = tempC,
            thermalState = thermalState,
        )
        return CollectedHealth(resources, memPct, cpuPct, tempC, statusLevel)
    }

    /**
     * 请求路径用的轻量热信号（不做 CPU/内存采样）：供发热降载闸门判断。
     * 任一读取失败都为 null，绝不抛异常，也绝不把 null 当成过热。
     */
    fun readThermalSignal(context: Context): ThermalSignal =
        ThermalSignal(statusLevel = readThermalStatusLevel(context), tempC = readTemperatureC(context))

    private fun readBatteryPct(context: Context): Double? = runCatching {
        val bm = context.getSystemService(Context.BATTERY_SERVICE) as? BatteryManager ?: return null
        val cap = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        if (cap <= 0) null else cap.toDouble()
    }.getOrNull()

    /** 电池温度：系统广播 EXTRA_TEMPERATURE 单位为 1/10 °C → 除以 10。取不到降级 null。 */
    private fun readTemperatureC(context: Context): Double? = runCatching {
        val intent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED), Context.RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("DEPRECATION") context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        }
        val t = intent?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) ?: 0
        if (t <= 0) null else t / 10.0
    }.getOrNull()

    /**
     * 热状态：API 29+ 读 PowerManager 真实等级；老系统/读取失败 → null。
     * 注意：平台不支持时常见返回 `THERMAL_STATUS_NONE(0)`，那只表示「不热」，不是过热。
     */
    private fun readThermalStatusLevel(context: Context): Int? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return null
        return runCatching {
            val pm = context.getSystemService(Context.POWER_SERVICE) as? PowerManager
            pm?.currentThermalStatus
        }.getOrNull()
    }

    private fun mapThermalStatus(status: Int?): String? {
        if (status == null) return null
        return when (status) {
            PowerManager.THERMAL_STATUS_NONE,
            PowerManager.THERMAL_STATUS_LIGHT,
            PowerManager.THERMAL_STATUS_MODERATE -> "nominal"
            PowerManager.THERMAL_STATUS_SEVERE -> "warn"
            else -> "hot" // CRITICAL / SHUTDOWN / EMERGENCY
        }
    }

    private fun readMemory(context: Context): Triple<Long?, Long?, Double?> = runCatching {
        val am = context.getSystemService(Context.ACTIVITY_SERVICE) as? ActivityManager ?: return@runCatching null
        val mi = ActivityManager.MemoryInfo()
        am.getMemoryInfo(mi)
        val availMb = mi.availMem / (1024 * 1024)
        val totalMb = mi.totalMem / (1024 * 1024)
        val pct = if (mi.totalMem > 0) (mi.totalMem - mi.availMem).toDouble() / mi.totalMem * 100.0 else null
        Triple(availMb, totalMb, pct)
    }.getOrNull() ?: Triple(null, null, null)

    /** CPU 使用率：两次采样 /proc/stat 之差。失败 → null。 */
    private suspend fun readCpuPct(): Double? = runCatching {
        val first = readProcStat() ?: return null
        delay(200)
        val second = readProcStat() ?: return null
        val totalDelta = (second.total - first.total).toDouble()
        val idleDelta = (second.idle - first.idle).toDouble()
        if (totalDelta <= 0) null else ((1.0 - idleDelta / totalDelta) * 100.0).coerceIn(0.0, 100.0)
    }.getOrNull()

    private data class CpuSample(val total: Long, val idle: Long)

    private fun readProcStat(): CpuSample? = runCatching {
        val line = File("/proc/stat").bufferedReader().use { it.readLine() } ?: return null
        val cols = line.split(Regex("\\s+")).drop(1).mapNotNull { it.toLongOrNull() }
        if (cols.isEmpty()) return null
        val idle = cols.getOrElse(3) { 0L }
        val total = cols.sum()
        CpuSample(total, idle)
    }.getOrNull()

    private fun readDiskFreeMb(context: Context): Long? = runCatching {
        val stat = StatFs(context.filesDir.absolutePath)
        val avail = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.JELLY_BEAN_MR2) {
            stat.availableBytes
        } else {
            @Suppress("DEPRECATION") stat.availableBlocks * stat.blockSize.toLong()
        }
        avail / (1024 * 1024)
    }.getOrNull()
}
