package com.biyou.sanxian

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log
import com.biyou.sanxian.node.EmbeddingEngine
import com.biyou.sanxian.node.NodeConfig
import com.biyou.sanxian.node.NodeServer
import com.biyou.sanxian.node.loadNodeConfig
import com.biyou.sanxian.llm.LlmConfig
import com.biyou.sanxian.llm.LlmEngineProvider
import com.biyou.sanxian.ui.getModelsDirectory
import java.io.File

/**
 * 散仙节点的前台服务：进程保活 + 启动 Ktor 服务 + 模型预热。
 *
 * 对应准圣的 lifespan（启动探活 + 预热白名单）。差异：
 *  - 准圣探活 = Ollama /v1/models；散仙引擎单例在 [LlmEngineProvider] 构造即就绪。
 *  - 准圣预热 = Ollama keep_alive=-1；散仙 = 对白名单首项调 [LlmEngine.loadModel]
 *    （注意：当前 [com.biyou.sanxian.llm.LlmEngine] 为单句柄引擎，**同一时刻仅一个模型常驻**，
 *    多模型轮换属后续工作；V1 只预热首项）。
 */
class NodeForegroundService : Service() {

    private var server: NodeServer? = null

    override fun onCreate() {
        super.onCreate()
        startForeground(NOTIF_ID, buildNotification())

        val config = loadConfig()
        val engine = LlmEngineProvider.engine

        // 预热：chat 模型（preload 首项）+ 独立 embedding 模型（若配置）。
        // 两者路径均已解析为应用私有目录下的绝对路径；embedding 模型经 LlmConfig.embeddingModelPath
        // 传入，由 AndroidLlmEngine 加载到独立句柄；为空则节点不承接 embedding（/v1/embeddings 走 501/503）。
        val chatPath = config.preloadModels.firstOrNull()
        if (chatPath != null) {
            val result = runCatching {
                engine.loadModel(
                    LlmConfig(
                        modelPath = chatPath,
                        embeddingModelPath = config.embeddingModel,
                    ),
                )
            }
            if (result.isSuccess) {
                Log.i(TAG, "预热完成：chat=$chatPath，embedding=${config.embeddingModel ?: "未配置"}")
                if (embeddingConfigured && config.embeddingModel == null) notifyModelMissing("embedding 模型未就绪，请导入 GGUF")
            } else {
                Log.e(TAG, "预热失败：$chatPath", result.exceptionOrNull())
                notifyModelMissing("模型加载失败，请在 App「模型管理」中重新导入")
            }
        } else {
            Log.w(TAG, "未找到可预热的 chat 模型，节点以无模型状态启动")
            notifyModelMissing("模型未就绪：请在 App「模型管理」中导入 GGUF")
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

    private var embeddingConfigured = false

    /**
     * 读取 assets 配置并把模型路径解析为绝对路径：
     * 配置里只写文件名（如 `Qwen3-0.6B-Q8_0.gguf`），统一落到 [getModelsDirectory]（应用私有目录）。
     * 找不到时逐项打 WARN 并交由 [notifyModelMissing] 提示导入，绝不静默降级。
     */
    private fun loadConfig(): NodeConfig {
        val raw = runCatching {
            assets.open("sanxian.json").bufferedReader().use { loadNodeConfig(it.readText()) }
        }.getOrElse {
            Log.e(TAG, "读取 sanxian.json 失败，回退默认配置", it)
            NodeConfig()
        }
        embeddingConfigured = raw.embeddingModel != null
        return raw.copy(
            preloadModels = raw.preloadModels.mapNotNull { resolveModel(it, "chat") },
            embeddingModel = raw.embeddingModel?.let { resolveModel(it, "embedding") },
        )
    }

    private fun resolveModel(entry: String, role: String): String? {
        val name = File(entry).name
        val target = File(getModelsDirectory(), name)
        if (target.isFile) return target.absolutePath
        val legacy = File(LEGACY_MODELS_DIR, name)
        if (legacy.isFile) {
            Log.w(TAG, "$role 模型 $name 只存在于旧目录 $LEGACY_MODELS_DIR，分区存储下 App 不可读；请在「模型管理」中导入 GGUF")
        } else {
            Log.w(TAG, "$role 模型 $name 不存在（查找目录 ${getModelsDirectory()}）；请在「模型管理」中导入 GGUF")
        }
        return null
    }

    private fun notifyModelMissing(text: String) {
        runCatching {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            nm.notify(NOTIF_ID, buildNotification(text))
        }
    }

    private fun buildNotification(contentText: String = "本地推理服务已对外暴露"): Notification {
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
            .setContentText(contentText)
            .setSmallIcon(android.R.drawable.ic_dialog_email)
            .setOngoing(true)
            .build()
    }

    companion object {
        const val NOTIF_ID = 1001
        private const val TAG = "NodeForegroundService"

        /** 旧版默认模型目录：仅用于「存在但不可读」的诊断提示，不再作为加载路径。 */
        private const val LEGACY_MODELS_DIR = "/sdcard/biyou/models"

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
