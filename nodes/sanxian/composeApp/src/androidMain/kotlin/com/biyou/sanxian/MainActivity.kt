package com.biyou.sanxian

import android.Manifest
import android.app.ActivityManager
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
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.core.view.WindowCompat
import com.biyou.sanxian.NodeForegroundService
import com.biyou.sanxian.ui.SanXianApp

class MainActivity : ComponentActivity() {

    /** 前台启动服务被系统拒绝时置 true，用对话框明确告知用户，而不是静默失败。 */
    private var serviceStartFailed by mutableStateOf(false)

    /** Android 13+ 通知需运行时授权：看护任务只能靠通知提醒用户，缺权限会变成静默失败。 */
    private val notificationPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) {}

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        requestNotificationPermissionIfNeeded()

        // 后台被系统限制时，WorkManager 看护任务连执行机会都没有，属于最隐蔽的失效场景，
        // 必须显式提示（仅靠电池白名单文案会误导用户去改一个不生效的开关）。
        val bgRestricted = isBackgroundRestricted()
        val needWhitelist = bgRestricted || !isIgnoringBatteryOptimizations()
        setContent {
            SanXianApp(darkTheme = isSystemInDarkTheme())
            var showWhitelist by remember { mutableStateOf(needWhitelist) }
            if (showWhitelist) {
                AlertDialog(
                    onDismissRequest = { showWhitelist = false },
                    title = { Text(if (bgRestricted) "节点后台运行被限制" else "保持节点后台运行") },
                    text = {
                        Text(
                            if (bgRestricted) {
                                "系统当前限制了「散仙」的后台运行，看护任务无法执行，节点被停掉后也不会提醒。" +
                                    "请在 设置 → 应用 → 散仙 → 电池 中关闭后台限制（或选择「不受限制」）。"
                            } else {
                                "设备空闲时系统可能回收本应用的后台服务（此前熄屏 6 分钟即被 app idle 回收）。" +
                                    "将「散仙」加入电池优化白名单可避免被回收。"
                            },
                        )
                    },
                    confirmButton = {
                        TextButton(onClick = {
                            showWhitelist = false
                            if (bgRestricted) openAppSettings() else requestIgnoreBatteryOptimizations()
                        }) { Text(if (bgRestricted) "应用设置" else "去设置") }
                    },
                    dismissButton = {
                        TextButton(onClick = { showWhitelist = false }) { Text("稍后") }
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
    }

    /**
     * 每次回到前台都确保服务在跑：这是系统停掉服务后唯一稳定可靠的恢复路径
     * （后台自愈受限，见 [NodeWatchdogWorker]）。启动失败时置状态弹对话框。
     */
    override fun onStart() {
        super.onStart()
        startNodeService()
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

    private fun isIgnoringBatteryOptimizations(): Boolean {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        return pm.isIgnoringBatteryOptimizations(packageName)
    }

    /** 用户/系统在「电池」里给本应用设了「限制后台运行」；此时 WorkManager 任务不会被执行。 */
    private fun isBackgroundRestricted(): Boolean =
        (getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager).isBackgroundRestricted

    private fun openAppSettings() {
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
