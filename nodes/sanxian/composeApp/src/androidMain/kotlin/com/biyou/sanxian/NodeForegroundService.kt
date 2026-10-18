package com.biyou.sanxian

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import com.biyou.sanxian.node.EmbeddingEngine
import com.biyou.sanxian.node.NodeConfig
import com.biyou.sanxian.node.NodeServer
import com.biyou.sanxian.node.loadNodeConfig
import com.smallphone.llm.LlmConfig
import com.smallphone.llm.LlmEngineProvider

/**
 * 散仙节点的前台服务：进程保活 + 启动 Ktor 服务 + 模型预热。
 *
 * 对应准圣的 lifespan（启动探活 + 预热白名单）。差异：
 *  - 准圣探活 = Ollama /v1/models；散仙引擎单例在 [LlmEngineProvider] 构造即就绪。
 *  - 准圣预热 = Ollama keep_alive=-1；散仙 = 对白名单首项调 [LlmEngine.loadModel]
 *    （注意：当前 [com.smallphone.llm.LlmEngine] 为单句柄引擎，**同一时刻仅一个模型常驻**，
 *    多模型轮换属后续工作；V1 只预热首项）。
 */
class NodeForegroundService : Service() {

    private var server: NodeServer? = null

    override fun onCreate() {
        super.onCreate()
        startForeground(NOTIF_ID, buildNotification())

        val config = loadConfig()
        val engine = LlmEngineProvider.engine

        // 预热：白名单首项（单句柄引擎，仅一项）
        config.preloadModels.firstOrNull()?.let { path ->
            runCatching { engine.loadModel(LlmConfig(modelPath = path)) }
                .onFailure { it.printStackTrace() }
        }

        val embedding = engine as? EmbeddingEngine
        server = NodeServer(this, config, engine, embedding)
        server?.start()
    }

    override fun onDestroy() {
        server?.stop()
        server = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun loadConfig(): NodeConfig = runCatching {
        assets.open("sanxian.json").bufferedReader().use { loadNodeConfig(it.readText()) }
    }.getOrElse { NodeConfig() }

    private fun buildNotification(): Notification {
        val channelId = "sanxian_node"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val chan = NotificationChannel(
                channelId, "散仙节点服务", NotificationManager.IMPORTANCE_LOW
            ).apply { description = "BiYouPalace 散仙推理节点" }
            (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
                .createNotificationChannel(chan)
        }
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, channelId)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setContentTitle("散仙节点运行中")
            .setContentText("本地推理服务已对外暴露")
            .setSmallIcon(android.R.drawable.ic_dialog_email)
            .setOngoing(true)
            .build()
    }

    companion object {
        const val NOTIF_ID = 1001

        /** 拉起前台服务（由 Application 或 UI 显式调用）。 */
        fun start(context: Context) {
            val intent = Intent(context, NodeForegroundService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }
    }
}
