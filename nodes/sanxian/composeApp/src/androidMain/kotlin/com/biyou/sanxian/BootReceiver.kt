package com.biyou.sanxian

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * 开机自启：BOOT_COMPLETED（以及覆盖安装后的 MY_PACKAGE_REPLACED）拉起节点前台服务。
 *
 * Android 12+ 把 BOOT_COMPLETED 列为后台启动前台服务的豁免场景，可直接 startForegroundService；
 * Android 15 禁止从 BOOT_COMPLETED 拉起 dataSync 类型，因此服务在新系统上以 specialUse 启动
 * （见 [NodeForegroundService.startForegroundCompat]）。
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED -> {
                Log.i(TAG, "收到 ${intent.action}，拉起节点前台服务")
                NodeForegroundService.start(context)
            }
        }
    }

    private companion object {
        const val TAG = "BootReceiver"
    }
}
