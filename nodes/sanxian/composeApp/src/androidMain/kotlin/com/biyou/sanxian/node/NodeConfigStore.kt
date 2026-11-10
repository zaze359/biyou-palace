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

    /** 合并后的生效配置（模型仍为文件名或 assets 里的相对路径，由服务解析为绝对路径）。 */
    suspend fun effectiveConfig(context: Context): NodeConfig {
        val app = context.applicationContext
        val assets = readAssetsConfig(app)
        val prefs = app.nodeConfigDataStore.data.first()
        val token = resolveToken(app, assets, prefs)

        val chatModels = if (prefs.contains(KEY_CHAT_MODEL)) {
            prefs[KEY_CHAT_MODEL]?.takeIf { it.isNotBlank() }?.let { listOf(it) } ?: emptyList()
        } else {
            assets.preloadModels
        }
        val embedding = if (prefs.contains(KEY_EMBEDDING_MODEL)) {
            prefs[KEY_EMBEDDING_MODEL]?.takeIf { it.isNotBlank() }
        } else {
            assets.embeddingModel
        }

        // 用户选了 assets 里没登记过的模型时，补齐能力声明，否则 /v1/embeddings 会因「未声明能力」被拒。
        val capabilities = assets.capabilitiesMap.toMutableMap()
        chatModels.firstOrNull()?.let { name ->
            if (capabilities[name] == null) capabilities[name] = ModelCapability(listOf("chat"))
        }
        embedding?.let { name ->
            capabilities[name] = ModelCapability(listOf("embedding"), capabilities[name]?.contextLen)
        }

        return assets.copy(
            port = prefs[KEY_PORT] ?: assets.port,
            sharedToken = token,
            authEnabled = prefs[KEY_AUTH] ?: assets.authEnabled,
            maxConcurrency = prefs[KEY_CONCURRENCY] ?: assets.maxConcurrency,
            preloadModels = chatModels,
            embeddingModel = embedding,
            capabilitiesMap = capabilities,
        )
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
