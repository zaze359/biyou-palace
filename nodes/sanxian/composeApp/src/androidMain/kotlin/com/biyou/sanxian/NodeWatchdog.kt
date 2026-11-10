package com.biyou.sanxian

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.biyou.sanxian.node.NodeConfigStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.TimeUnit

/**
 * 节点看护：周期探活 `/v1/health`，发现服务已停时尝试自愈，失败则发**用户可见**提醒。
 *
 * 为什么需要它：节点以 FGS 常驻，但系统仍可能在两种情况下把服务停掉——
 *  1) 应用进入 idle 状态被回收（熄屏后 `Stopping service due to app idle`）；
 *  2) 进程被杀后 `START_STICKY` 重建失败（如后台启动 FGS 被拒）。
 * 这两种情况下服务自己没有任何机会再上报，只能由外部周期任务兜底。
 *
 * ## Android 12 上的真实能力边界（不要指望它替代用户操作）
 * - **周期下限 15 分钟**：WorkManager 的 `PeriodicWorkRequest` 最短周期就是 15 分钟，
 *   熄屏/Doze 下还会被推迟到维护窗口（未加电池白名单时可能是数小时）。
 * - **后台启动 FGS 受限**：Android 12 起，应用在后台调用 `startForegroundService()` 会抛
 *   `ForegroundServiceStartNotAllowedException`；WorkManager 的 `setForeground` 同样受限。
 *   因此本 Worker 的「自愈」只能覆盖系统恰好给了豁免窗口的场景，**不能保证成功**。
 * - 失败时不做任何 hack（不用空通知循环、不用 1px Activity 保活），而是发通知明确告知
 *   「请打开 App 保持前台」——打开 App 时由 [MainActivity] 在前台态重新拉起服务，
 *   这是唯一稳定可靠的恢复路径。
 */
class NodeWatchdogWorker(
    context: Context,
    params: WorkerParameters,
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val port = readPort()
        if (probe(port)) {
            Log.i(TAG, "探活 127.0.0.1:$port 正常")
            return Result.success()
        }

        Log.w(TAG, "探活 127.0.0.1:$port 无响应，尝试拉起前台服务")
        NodeForegroundService.start(applicationContext)
        // 后台启动是否被拒有两种表现：同步抛异常，或系统只打日志、静默不创建服务。
        // 因此不能只看返回值，必须再探一次端口。
        delay(RETRY_DELAY_MS)
        if (probe(port)) {
            Log.i(TAG, "自愈成功，节点已恢复")
            return Result.success()
        }

        Log.w(TAG, "自愈失败：后台启动前台服务被系统拒绝，发提醒通知")
        notifyStopped()
        return Result.success()
    }

    private fun readPort(): Int = runCatching {
        // 与前台服务同源：DataStore 用户配置 > assets 默认值。端口可能在设置页改过。
        NodeConfigStore.effectiveConfigBlocking(applicationContext).port
    }.getOrDefault(DEFAULT_PORT)

    /** 只探本机回环，不需要网络；2s 超时，避免 Worker 被网络栈拖住。 */
    private suspend fun probe(port: Int): Boolean = withContext(Dispatchers.IO) {
        runCatching {
            val conn = URL("http://127.0.0.1:$port/v1/health").openConnection() as HttpURLConnection
            conn.connectTimeout = 2000
            conn.readTimeout = 2000
            conn.requestMethod = "GET"
            try {
                conn.responseCode == HttpURLConnection.HTTP_OK
            } finally {
                conn.disconnect()
            }
        }.getOrDefault(false)
    }

    private fun notifyStopped() {
        runCatching {
            val nm = applicationContext.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                nm.createNotificationChannel(
                    NotificationChannel(CHANNEL_ID, "散仙节点异常", NotificationManager.IMPORTANCE_DEFAULT)
                        .apply { description = "节点被系统停止时的提醒" },
                )
            }
            val open = PendingIntent.getActivity(
                applicationContext,
                0,
                Intent(applicationContext, MainActivity::class.java).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                },
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
            val text = "系统已停止节点服务，且后台自愈被系统限制。请点开「散仙」并保持前台运行以恢复节点。"
            nm.notify(
                ALERT_ID,
                NotificationCompat.Builder(applicationContext, CHANNEL_ID)
                    .setSmallIcon(android.R.drawable.stat_notify_error)
                    .setContentTitle("散仙节点已停止")
                    .setContentText(text)
                    .setStyle(NotificationCompat.BigTextStyle().bigText(text))
                    .setContentIntent(open)
                    .setAutoCancel(true)
                    .build(),
            )
        }.onFailure { Log.e(TAG, "发送节点停止提醒失败", it) }
    }

    companion object {
        private const val TAG = "NodeWatchdog"
        private const val UNIQUE_NAME = "sanxian_node_watchdog"
        private const val CHANNEL_ID = "sanxian_node_alert"
        private const val ALERT_ID = 1002
        private const val RETRY_DELAY_MS = 3000L
        private const val DEFAULT_PORT = 4011

        /** 注册周期看护（幂等：KEEP 不会重复排队）。由 [SanXianApplication] 在进程创建时调用。 */
        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<NodeWatchdogWorker>(15, TimeUnit.MINUTES).build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                UNIQUE_NAME,
                ExistingPeriodicWorkPolicy.KEEP,
                request,
            )
        }

        /** 节点恢复后撤掉「已停止」提醒，避免用户看到过期状态。 */
        fun clearAlert(context: Context) {
            runCatching {
                (context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).cancel(ALERT_ID)
            }.onFailure { Log.w(TAG, "撤销节点停止提醒失败", it) }
        }
    }
}
