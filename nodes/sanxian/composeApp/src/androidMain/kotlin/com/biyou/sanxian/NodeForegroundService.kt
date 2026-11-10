package com.biyou.sanxian

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.util.Log
import com.biyou.sanxian.node.EmbeddingEngine
import com.biyou.sanxian.node.NodeConfig
import com.biyou.sanxian.node.NodeConfigStore
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

    /** 熄屏存活用的 CPU 锁与 Wi-Fi 锁，随服务生命周期持有/释放。 */
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    override fun onCreate() {
        super.onCreate()
        // 后台启动被系统拒绝时，onCreate 可能仍被调用但 startForeground 会抛异常；
        // 不捕获会变成「did not call startForeground」ANR/崩溃。失败即退出，由看护任务提醒用户。
        if (!startForegroundCompat()) {
            Log.e(TAG, "startForeground 失败，服务退出（Android 12+ 后台启动前台服务受限）")
            stopSelf()
            return
        }
        acquireLocks()
        // 节点已恢复：撤掉看护任务此前发出的「已停止」提醒
        NodeWatchdogWorker.clearAlert(this)

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

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // 每次 startForegroundService（Activity 回前台、看护任务重试）都会新开一张
        // 「必须很快调用 startForeground」的票据。即使服务已在前台，也必须再调一次，
        // 否则系统抛 ForegroundServiceDidNotStartInTimeException 直接杀进程。
        // startForeground 幂等，重复调用只是刷新通知。
        if (!startForegroundCompat()) {
            Log.e(TAG, "onStartCommand 中 startForeground 失败，服务退出")
            stopSelf()
            return START_NOT_STICKY
        }
        // START_STICKY：进程被系统回收后由系统重建服务（onCreate 会重新加载模型并重持锁）；
        // 不返回 START_REDELIVER_INTENT，避免重复投递已处理的 Intent。
        Log.i(TAG, "onStartCommand startId=$startId，返回 START_STICKY")
        return START_STICKY
    }

    override fun onDestroy() {
        releaseLocks()
        server?.stop()
        server = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    /**
     * API 34+ 以 specialUse 类型启动：Android 15 对 dataSync 前台服务有 6 小时上限，
     * 且不允许从 BOOT_COMPLETED 拉起 dataSync；specialUse 无这两项限制。
     * Android 12/13（本机）走两参重载，沿用 manifest 中的 dataSync，行为与之前一致。
     */
    private fun startForegroundCompat(): Boolean = runCatching {
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, buildNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIF_ID, buildNotification())
        }
    }.onFailure {
        Log.e(TAG, "startForeground 抛异常（后台启动前台服务被拒）", it)
    }.isSuccess

    /**
     * 熄屏后保持 CPU 与 Wi-Fi 活跃，保证 HTTP 服务可持续响应。
     * 注意：锁只能防设备休眠，不能防「app idle 回收」——后者需靠电池优化白名单，
     * 由 MainActivity 在未白名单时弹窗引导。
     */
    private fun acquireLocks() {
        runCatching {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "$TAG:cpu").apply {
                setReferenceCounted(false)
                acquire()
            }
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            // HIGH_PERF 在部分新系统上被限流，但仍是熄屏维持 Wi-Fi 收发的标准做法。
            wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "$TAG:wifi").apply {
                setReferenceCounted(false)
                acquire()
            }
            Log.i(TAG, "已持有 partial WakeLock + WifiLock（熄屏保持 CPU/Wi-Fi 活跃）")
        }.onFailure { Log.e(TAG, "获取 WakeLock/WifiLock 失败", it) }
    }

    private fun releaseLocks() {
        runCatching { if (wakeLock?.isHeld == true) wakeLock?.release() }
            .onFailure { Log.w(TAG, "释放 WakeLock 失败", it) }
        runCatching { if (wifiLock?.isHeld == true) wifiLock?.release() }
            .onFailure { Log.w(TAG, "释放 WifiLock 失败", it) }
        wakeLock = null
        wifiLock = null
    }

    private var embeddingConfigured = false

    /**
     * 读取生效配置（DataStore 用户配置 > assets 默认值），并把模型路径解析为绝对路径：
     * 配置里只写文件名（如 `Qwen3-0.6B-Q8_0.gguf`），统一落到 [getModelsDirectory]（应用私有目录）。
     * 找不到时逐项打 WARN 并交由 [notifyModelMissing] 提示导入，绝不静默降级。
     */
    private fun loadConfig(): NodeConfig {
        val raw = runCatching { NodeConfigStore.effectiveConfigBlocking(this) }.getOrElse {
            Log.e(TAG, "读取 DataStore 配置失败，回退 assets 默认配置", it)
            runCatching {
                assets.open("sanxian.json").bufferedReader().use { loadNodeConfig(it.readText()) }
            }.getOrDefault(NodeConfig())
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

        /** 等旧实例 onDestroy（停服务、释放端口/模型）完成后再拉起新实例。 */
        private const val RESTART_DELAY_MS = 400L

        /** 旧版默认模型目录：仅用于「存在但不可读」的诊断提示，不再作为加载路径。 */
        private const val LEGACY_MODELS_DIR = "/sdcard/biyou/models"

        /**
         * 拉起前台服务（由 Activity 前台态或开机广播调用）。
         *
         * @return false 表示调用侧被同步拒绝（Android 12+ 后台启动 FGS 抛
         *   ForegroundServiceStartNotAllowedException）。注意：系统也可能只打日志、
         *   静默拒绝而不抛异常，因此调用方必须用「端口探活」确认结果，不能只看返回值。
         */
        fun start(context: Context): Boolean = runCatching {
            val intent = Intent(context, NodeForegroundService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
            true
        }.onFailure {
            Log.e(TAG, "拉起前台服务失败（Android 12+ 后台禁止启动 FGS）", it)
        }.getOrDefault(false)

        /**
         * 重启服务使新配置生效（端口 / token / 鉴权 / 并发 / 模型均在 onCreate 读取）。
         * 先 stopService 释放旧端口与模型句柄，稍后在前台态重新拉起。
         * 调用方须处于前台（设置页保存时），否则可能被 Android 12+ 拒绝后台启动 FGS。
         */
        fun restart(context: Context): Boolean {
            val app = context.applicationContext
            val stopped = runCatching {
                app.stopService(Intent(app, NodeForegroundService::class.java))
            }.onFailure { Log.e(TAG, "停止旧服务失败", it) }.getOrDefault(false)
            Handler(Looper.getMainLooper()).postDelayed({ start(app) }, RESTART_DELAY_MS)
            Log.i(TAG, "服务重启中（旧实例已停止=$stopped，${RESTART_DELAY_MS}ms 后重新拉起）")
            return true
        }
    }
}
