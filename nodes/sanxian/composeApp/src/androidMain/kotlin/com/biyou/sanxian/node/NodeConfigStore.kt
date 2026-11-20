package com.biyou.sanxian.node

import android.content.Context
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import java.security.SecureRandom
import java.util.Base64

/**
 * 设备端配置存储（DataStore Preferences）。
 *
 * 生效优先级：**DataStore 用户配置 > assets/sanxian.json 默认值 > [NodeConfig] 内置默认**。
 * 前台服务与看护任务都通过 [effectiveConfig] 读取，改配置后重启服务即可生效，无需重打包。
 *
 * 约定：
 *  - 只有用户保存过的键才覆盖 assets；未保存的键继续使用 assets 值（便于升级默认值）。
 *  - `shared_token` 首次读取时若 DataStore 与 assets 都没有，则生成随机值并持久化；
 *    代码里不存在任何默认 token 常量。
 */
private val Context.nodeConfigDataStore by preferencesDataStore(name = "sanxian_node_config")

object NodeConfigStore {

    private val KEY_PORT = intPreferencesKey("port")
    private val KEY_TOKEN = stringPreferencesKey("shared_token")
    private val KEY_AUTH = booleanPreferencesKey("auth_enabled")
    private val KEY_CONCURRENCY = intPreferencesKey("max_concurrency")
    private val KEY_CHAT_MODEL = stringPreferencesKey("chat_model")
    private val KEY_EMBEDDING_MODEL = stringPreferencesKey("embedding_model")

    // 集群（阵图）配置：仅节点前端使用，不参与节点服务启动
    private val KEY_ZHENTU_URL = stringPreferencesKey("zhentu_base_url")
    private val KEY_ZHENTU_TOKEN = stringPreferencesKey("zhentu_token")
    private val KEY_ORIGIN_NODE_ID = stringPreferencesKey("origin_node_id")
    private val KEY_DEFAULT_MODE = stringPreferencesKey("default_mode")

    // 集群任务摘要：断线重连只需 task_id + 事件游标 + 终态答案
    private val KEY_CLUSTER_TASK_ID = stringPreferencesKey("cluster_task_id")
    private val KEY_CLUSTER_TASK_EVENT_ID = stringPreferencesKey("cluster_task_event_id")
    private val KEY_CLUSTER_TASK_STATUS = stringPreferencesKey("cluster_task_status")
    private val KEY_CLUSTER_TASK_ANSWER = stringPreferencesKey("cluster_task_answer")

    /** 合并后的生效配置（模型仍为文件名或 assets 里的相对路径，由服务解析为绝对路径）。 */
    suspend fun effectiveConfig(context: Context): NodeConfig {
        val app = context.applicationContext
        val assets = readAssetsConfig(app)
        val prefs = app.nodeConfigDataStore.data.first()

        // 只把「用户保存过的键」作为覆盖项，其余沿用 assets；合并规则本身在 shared 层可单测。
        val overrides = UserConfigOverrides(
            port = prefs[KEY_PORT],
            sharedToken = resolveToken(app, assets, prefs),
            authEnabled = prefs[KEY_AUTH],
            maxConcurrency = prefs[KEY_CONCURRENCY],
            chatModel = if (prefs.contains(KEY_CHAT_MODEL)) prefs[KEY_CHAT_MODEL].orEmpty() else null,
            embeddingModel = if (prefs.contains(KEY_EMBEDDING_MODEL)) prefs[KEY_EMBEDDING_MODEL].orEmpty() else null,
        )
        return assets.withUserOverrides(overrides)
    }

    /** 同步版本，供 Service / Worker 这类无法挂起但需要立即拿配置的入口使用。 */
    fun effectiveConfigBlocking(context: Context): NodeConfig = runBlocking { effectiveConfig(context) }

    /** 写入全部用户可编辑项；空字符串表示「显式关闭」（如不启用 embedding）。 */
    suspend fun save(
        context: Context,
        port: Int,
        sharedToken: String,
        authEnabled: Boolean,
        maxConcurrency: Int,
        chatModel: String?,
        embeddingModel: String?,
    ) {
        context.applicationContext.nodeConfigDataStore.edit { prefs ->
            prefs[KEY_PORT] = port
            prefs[KEY_TOKEN] = sharedToken
            prefs[KEY_AUTH] = authEnabled
            prefs[KEY_CONCURRENCY] = maxConcurrency
            prefs[KEY_CHAT_MODEL] = chatModel.orEmpty()
            prefs[KEY_EMBEDDING_MODEL] = embeddingModel.orEmpty()
        }
    }

    private suspend fun resolveToken(
        context: Context,
        assets: NodeConfig,
        prefs: Preferences,
    ): String {
        prefs[KEY_TOKEN]?.takeIf { it.isNotBlank() }?.let { return it }
        assets.sharedToken?.takeIf { it.isNotBlank() }?.let { return it }
        val generated = generateToken()
        context.nodeConfigDataStore.edit { it[KEY_TOKEN] = generated }
        return generated
    }

    /** 阵图连接配置；`originNodeId` 为空时回退 assets/NodeConfig 的 node_id。 */
    suspend fun clusterConfig(context: Context): StoredClusterConfig {
        val app = context.applicationContext
        val prefs = app.nodeConfigDataStore.data.first()
        val defaultNodeId = readAssetsConfig(app).nodeId
        return StoredClusterConfig(
            baseUrl = prefs[KEY_ZHENTU_URL].orEmpty().trimEnd('/'),
            token = prefs[KEY_ZHENTU_TOKEN].orEmpty(),
            originNodeId = prefs[KEY_ORIGIN_NODE_ID]?.takeIf { it.isNotBlank() } ?: defaultNodeId,
            defaultMode = prefs[KEY_DEFAULT_MODE] ?: "local",
        )
    }

    suspend fun saveCluster(
        context: Context,
        baseUrl: String,
        token: String,
        originNodeId: String,
        defaultMode: String,
    ) {
        context.applicationContext.nodeConfigDataStore.edit { prefs ->
            prefs[KEY_ZHENTU_URL] = baseUrl.trim().trimEnd('/')
            prefs[KEY_ZHENTU_TOKEN] = token.trim()
            prefs[KEY_ORIGIN_NODE_ID] = originNodeId.trim()
            prefs[KEY_DEFAULT_MODE] = defaultMode
        }
    }

    /** 读取上次集群任务摘要；没有任务返回 null。 */
    suspend fun clusterTask(context: Context): StoredClusterTask? {
        val prefs = context.applicationContext.nodeConfigDataStore.data.first()
        val taskId = prefs[KEY_CLUSTER_TASK_ID]?.takeIf { it.isNotBlank() } ?: return null
        return StoredClusterTask(
            taskId = taskId,
            lastEventId = prefs[KEY_CLUSTER_TASK_EVENT_ID]?.takeIf { it.isNotBlank() },
            status = prefs[KEY_CLUSTER_TASK_STATUS].orEmpty(),
            answer = prefs[KEY_CLUSTER_TASK_ANSWER],
        )
    }

    suspend fun saveClusterTask(context: Context, task: StoredClusterTask) {
        context.applicationContext.nodeConfigDataStore.edit { prefs ->
            prefs[KEY_CLUSTER_TASK_ID] = task.taskId
            prefs[KEY_CLUSTER_TASK_EVENT_ID] = task.lastEventId.orEmpty()
            prefs[KEY_CLUSTER_TASK_STATUS] = task.status
            prefs[KEY_CLUSTER_TASK_ANSWER] = task.answer.orEmpty()
        }
    }

    suspend fun clearClusterTask(context: Context) {
        context.applicationContext.nodeConfigDataStore.edit { prefs ->
            prefs.remove(KEY_CLUSTER_TASK_ID)
            prefs.remove(KEY_CLUSTER_TASK_EVENT_ID)
            prefs.remove(KEY_CLUSTER_TASK_STATUS)
            prefs.remove(KEY_CLUSTER_TASK_ANSWER)
        }
    }

    private fun readAssetsConfig(context: Context): NodeConfig = runCatching {
        context.assets.open("sanxian.json").bufferedReader().use { loadNodeConfig(it.readText()) }
    }.getOrElse { NodeConfig() }

    /** 24 字节 SecureRandom → URL-safe base64（32 字符），足够作为共享访问令牌。 */
    fun generateToken(): String {
        val bytes = ByteArray(24)
        SecureRandom().nextBytes(bytes)
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
    }
}

/** 阵图连接配置的存储形状（纯字符串，UI 层再映射为 ChatMode）。 */
data class StoredClusterConfig(
    val baseUrl: String,
    val token: String,
    val originNodeId: String,
    val defaultMode: String,
)

/** 集群任务摘要：断线重连所需的最小状态。 */
data class StoredClusterTask(
    val taskId: String,
    val lastEventId: String?,
    val status: String,
    val answer: String?,
)
