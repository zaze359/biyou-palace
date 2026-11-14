package com.biyou.sanxian

import android.Manifest
import android.app.ActivityManager
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.core.view.WindowCompat
import androidx.lifecycle.lifecycleScope
import com.biyou.sanxian.ui.SanXianApp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 后台运行限制的三个信号。
 *
 * [fingerprint] 是「用户已点稍后」的确认键：只有三项都变化才会重新用强弹窗打扰。
 */
private data class BackgroundSignals(
    val restricted: Boolean,
    val ignoringBatteryOptimizations: Boolean,
    val standbyBucket: Int,
) {
    val fingerprint: String
        get() = "restricted=$restricted;ignoring=$ignoringBatteryOptimizations;bucket=$standbyBucket"

    val needsAttention: Boolean
        get() = restricted || !ignoringBatteryOptimizations
}

class MainActivity : ComponentActivity() {

    /** 前台启动服务被系统拒绝时置 true，用对话框明确告知用户，而不是静默失败。 */
    private var serviceStartFailed by mutableStateOf(false)

    private var bgSignals by mutableStateOf(BackgroundSignals(restricted = false, ignoringBatteryOptimizations = true, standbyBucket = 0))
    /** 只有 appop 受限（真正影响 WorkManager 看护）才用强弹窗。 */
    private var showBlockingDialog by mutableStateOf(false)
    /** 首页横幅的本次进程内关闭状态；信号指纹变化或解除后自动复位。 */
    private var bannerDismissed by mutableStateOf(false)
    private var initialPromptEvaluated = false

    /** Android 13+ 通知需运行时授权：看护任务只能靠通知提醒用户，缺权限会变成静默失败。 */
    private val notificationPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) {}

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        requestNotificationPermissionIfNeeded()

        bgSignals = readBackgroundSignals()
        setContent {
            SanXianApp(
                darkTheme = isSystemInDarkTheme(),
                banner = if (bgSignals.needsAttention && !bannerDismissed && !showBlockingDialog) {
                    {
                        BackgroundRestrictionBanner(
                            signals = bgSignals,
                            onOpenSettings = {
                                if (bgSignals.restricted) openBatterySettings() else requestIgnoreBatteryOptimizations()
                            },
                            onDismiss = { bannerDismissed = true },
                        )
                    }
                } else {
                    null
                },
            )
            if (showBlockingDialog && bgSignals.restricted) {
                AlertDialog(
                    onDismissRequest = { showBlockingDialog = false },
                    title = { Text("后台运行被限制") },
                    text = {
                        Text(
                            "系统限制了「散仙」的后台运行，WorkManager 看护任务无法执行：" +
                                "节点服务异常退出后可能不会被自动拉起。节点服务目前仍在前台运行，不影响这次使用。" +
                                "请在电池设置中把「散仙」设为「不受限制」。",
                        )
                    },
                    confirmButton = {
                        TextButton(onClick = {
                            showBlockingDialog = false
                            openBatterySettings()
                        }) { Text("电池设置") }
                    },
                    dismissButton = {
                        TextButton(onClick = {
                            showBlockingDialog = false
                            val fingerprint = bgSignals.fingerprint
                            lifecycleScope.launch(Dispatchers.IO) {
                                BackgroundPromptStore.acknowledge(this@MainActivity, fingerprint)
                            }
                        }) { Text("稍后") }
                    },
                )
            }
            if (serviceStartFailed) {
                AlertDialog(
                    onDismissRequest = { serviceStartFailed = false },
                    title = { Text("节点服务未能启动") },
                    text = {
                        Text(
                            "系统拒绝了前台服务启动（Android 12+ 对后台启动前台服务有限制）。" +
                                "请保持「散仙」在前台，并确认它仍留在「最近任务」中——" +
                                "从最近任务划掉或长时间切后台后，系统可能停止节点。",
                        )
                    },
                    confirmButton = {
                        TextButton(onClick = {
                            serviceStartFailed = false
                            startNodeService()
                        }) { Text("重试") }
                    },
                    dismissButton = {
                        TextButton(onClick = { serviceStartFailed = false }) { Text("知道了") }
                    },
                )
            }
        }

        lifecycleScope.launch { refreshBackgroundPrompt(isResume = false) }
    }

    /**
     * 每次回到前台都确保服务在跑：这是系统停掉服务后唯一稳定可靠的恢复路径
     * （后台自愈受限，见 [NodeWatchdogWorker]）。启动失败时置状态弹对话框。
     */
    override fun onStart() {
        super.onStart()
        startNodeService()
    }

    /**
     * 从设置页返回时复检：限制解除就自动收起横幅与弹窗，并清掉「稍后」确认记录，
     * 避免下次再拿旧指纹压住提示。
     */
    override fun onResume() {
        super.onResume()
        if (initialPromptEvaluated) {
            lifecycleScope.launch { refreshBackgroundPrompt(isResume = true) }
        }
    }

    private suspend fun refreshBackgroundPrompt(isResume: Boolean) {
        val current = withContext(Dispatchers.IO) { readBackgroundSignals() }
        val fingerprintChanged = current.fingerprint != bgSignals.fingerprint
        bgSignals = current
        if (fingerprintChanged) bannerDismissed = false

        if (!current.needsAttention) {
            showBlockingDialog = false
            withContext(Dispatchers.IO) { BackgroundPromptStore.clear(this@MainActivity) }
            initialPromptEvaluated = true
            return
        }
        if (!current.restricted) {
            // 只是缺 Doze 白名单：不弹窗，交给首页横幅。
            showBlockingDialog = false
            initialPromptEvaluated = true
            return
        }
        if (isResume) {
            // 回到前台只复检、不主动再弹；用户刚操作过设置，避免立刻二次打扰。
            showBlockingDialog = false
            initialPromptEvaluated = true
            return
        }
        val acknowledged = withContext(Dispatchers.IO) {
            BackgroundPromptStore.acknowledgedFingerprint(this@MainActivity)
        }
        showBlockingDialog = acknowledged != current.fingerprint
        initialPromptEvaluated = true
    }

    private fun startNodeService() {
        serviceStartFailed = !NodeForegroundService.start(this)
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    private fun readBackgroundSignals(): BackgroundSignals = BackgroundSignals(
        restricted = isBackgroundRestricted(),
        ignoringBatteryOptimizations = isIgnoringBatteryOptimizations(),
        standbyBucket = currentStandbyBucket(),
    )

    private fun isIgnoringBatteryOptimizations(): Boolean {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        return pm.isIgnoringBatteryOptimizations(packageName)
    }

    /** 用户/系统在「电池」里给本应用设了「限制后台运行」；此时 WorkManager 任务不会被执行。 */
    private fun isBackgroundRestricted(): Boolean =
        (getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager).isBackgroundRestricted

    private fun currentStandbyBucket(): Int {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P) return -1
        return runCatching {
            (getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager).appStandbyBucket
        }.getOrDefault(0) // 0 = BUCKET_UNKNOWN
    }

    /**
     * 优先打开应用电池页（部分 ROM 支持 APP_BATTERY_SETTINGS），其次退回电池优化列表；
     * 不再落到通用的应用信息页。
     */
    private fun openBatterySettings() {
        val candidates = listOf(
            Intent("android.settings.APP_BATTERY_SETTINGS")
                .setData(Uri.parse("package:$packageName"))
                .putExtra("android.provider.extra.APP_PACKAGE", packageName),
            Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS),
        )
        for (intent in candidates) {
            if (runCatching { startActivity(intent) }.isSuccess) return
        }
        runCatching {
            startActivity(
                Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")),
            )
        }
    }

    /** 弹系统「忽略电池优化」授权页；厂商 ROM 上若被拦截则退回电池优化列表页。 */
    private fun requestIgnoreBatteryOptimizations() {
        runCatching {
            startActivity(
                Intent(
                    Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:$packageName"),
                ),
            )
        }.onFailure {
            runCatching { startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) }
        }
    }
}

/** 首页非阻塞提示：不遮挡聊天，不阻断任何操作。 */
@Composable
private fun BackgroundRestrictionBanner(
    signals: BackgroundSignals,
    onOpenSettings: () -> Unit,
    onDismiss: () -> Unit,
) {
    Surface(tonalElevation = 1.dp) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(start = 12.dp, end = 4.dp, top = 4.dp, bottom = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = if (signals.restricted) {
                    "后台运行受限：看护任务不会执行，节点服务仍在运行。"
                } else {
                    "未加入电池优化白名单：熄屏后看护任务可能被延迟。"
                },
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.weight(1f),
            )
            TextButton(onClick = onOpenSettings) {
                Text(if (signals.restricted) "电池设置" else "去设置")
            }
            TextButton(onClick = onDismiss) { Text("关闭") }
        }
    }
}
