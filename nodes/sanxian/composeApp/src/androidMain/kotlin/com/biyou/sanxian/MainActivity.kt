package com.biyou.sanxian

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.core.view.WindowCompat
import com.biyou.sanxian.NodeForegroundService
import com.biyou.sanxian.ui.SanXianApp

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        // 前台态拉起散仙节点服务（暴露 Ktor HTTP 端点）。必须在 Activity 前台时启动，
        // 否则 Android 12+ 会因后台启动 dataSync 前台服务而抛 ForegroundServiceStartNotAllowedException。
        NodeForegroundService.start(this)
        setContent {
            SanXianApp(darkTheme = isSystemInDarkTheme())
        }
    }
}
