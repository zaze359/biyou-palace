package com.biyou.sanxian

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
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
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        // 前台态拉起散仙节点服务（暴露 Ktor HTTP 端点）。必须在 Activity 前台时启动，
        // 否则 Android 12+ 会因后台启动前台服务而抛 ForegroundServiceStartNotAllowedException。
        // （开机自启走 BootReceiver，属于系统豁免场景。）
        NodeForegroundService.start(this)

        val needWhitelist = !isIgnoringBatteryOptimizations()
        setContent {
            SanXianApp(darkTheme = isSystemInDarkTheme())
            var showWhitelist by remember { mutableStateOf(needWhitelist) }
            if (showWhitelist) {
                AlertDialog(
                    onDismissRequest = { showWhitelist = false },
                    title = { Text("保持节点后台运行") },
                    text = {
                        Text(
                            "设备空闲时系统可能回收本应用的后台服务（此前熄屏 6 分钟即被 app idle 回收）。" +
                                "将「散仙」加入电池优化白名单可避免被回收。",
                        )
                    },
                    confirmButton = {
                        TextButton(onClick = {
                            showWhitelist = false
                            requestIgnoreBatteryOptimizations()
                        }) { Text("去设置") }
                    },
                    dismissButton = {
                        TextButton(onClick = { showWhitelist = false }) { Text("稍后") }
                    },
                )
            }
        }
    }

    private fun isIgnoringBatteryOptimizations(): Boolean {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        return pm.isIgnoringBatteryOptimizations(packageName)
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
