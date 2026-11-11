package com.biyou.sanxian.ui

import androidx.compose.ui.graphics.ImageBitmap

/**
 * 设备端节点配置（设置页 / 首页访问信息）。
 *
 * 读取优先级：DataStore 用户配置 > assets/sanxian.json 默认值。
 * 修改后通过重启前台服务生效，不需要重新打包 APK。
 */

/** 用户在设置页可编辑的配置项。模型字段为模型目录下的文件名（GGUF），null 表示不启用。 */
data class NodeSettingsValues(
    val port: Int,
    val sharedToken: String,
    val authEnabled: Boolean,
    val maxConcurrency: Int,
    val chatModel: String?,
    val embeddingModel: String?,
)

/** 当前生效配置 + 模型目录中可供选择的 GGUF 文件（仅文件名）。 */
data class NodeSettingsSnapshot(
    val values: NodeSettingsValues,
    val availableModels: List<String>,
)

/**
 * 首页展示的访问信息。
 * @param host 当前 Wi-Fi IPv4；为 null 时 [reason] 说明取不到的原因
 * @param reason 取不到 IP 时的可读原因（未连 Wi-Fi / 无 IPv4 / 读取失败）
 */
data class NodeAccessInfo(
    val host: String?,
    val port: Int,
    val reason: String?,
) {
    /** 对外访问根地址，如 `http://192.168.1.23:4011/v1`；无 IP 时为 null。 */
    val baseUrl: String?
        get() = host?.let { "http://$it:$port/v1" }
}

/** 读取当前生效配置（DataStore > assets）与可选模型列表。 */
expect suspend fun loadNodeSettings(): NodeSettingsSnapshot

/** 保存用户配置到 DataStore（仅写用户覆盖项，assets 仍作为缺省值）。 */
expect suspend fun saveNodeSettings(values: NodeSettingsValues): NodeSettingsSnapshot

/** 生成随机 shared_token（不写死在仓库，也不复用默认值）。 */
expect fun generateSharedToken(): String

/** 读取当前 Wi-Fi IPv4 与生效端口，用于首页展示访问地址。 */
expect suspend fun currentNodeAccessInfo(): NodeAccessInfo

/** 把文本编码为二维码位图；失败返回 null（不影响地址文本展示）。 */
expect fun encodeQrCode(content: String, sizePx: Int): ImageBitmap?

/** 重启前台服务使配置生效（端口/鉴权/并发/模型均在服务启动时读取）。 */
expect fun restartNodeService(): Boolean

/** 探活本机 `/v1/health`，用于保存配置后确认服务已恢复。 */
expect suspend fun probeNodeHealth(port: Int): Boolean

/**
 * 读取本机聊天端点（`http://127.0.0.1:<端口>/v1` + token）。
 * 聊天改走节点 HTTP 接口后，端口与 token 必须与设备配置保持一致。
 */
expect suspend fun loadNodeChatEndpoint(): NodeChatEndpoint

/**
 * 受控切换节点 chat 模型：写入 DataStore 并重启前台服务，由服务在 onCreate 里重新加载模型。
 * **不直接操作进程内引擎句柄**，避免聊天侧切换模型把对外服务的模型关掉。
 * @return false 表示保存成功但服务重启请求被系统拒绝
 */
expect suspend fun activateNodeChatModel(fileName: String): Boolean
