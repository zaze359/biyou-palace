package com.biyou.sanxian.ui

import android.content.Context
import android.graphics.Bitmap
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import com.biyou.sanxian.NodeForegroundService
import com.biyou.sanxian.SanXianApplication
import com.biyou.sanxian.node.NodeConfigStore
import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.qrcode.QRCodeWriter
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.URL

/**
 * 设备端配置与访问信息的 Android 实现。
 * 配置读写统一走 [NodeConfigStore]（DataStore > assets），前台服务使用同一份逻辑，避免两套优先级。
 */

actual suspend fun loadNodeSettings(): NodeSettingsSnapshot {
    val config = NodeConfigStore.effectiveConfig(SanXianApplication.appContext)
    return NodeSettingsSnapshot(
        values = NodeSettingsValues(
            port = config.port,
            sharedToken = config.sharedToken.orEmpty(),
            authEnabled = config.authEnabled,
            maxConcurrency = config.maxConcurrency,
            chatModel = config.preloadModels.firstOrNull()?.let { File(it).name },
            embeddingModel = config.embeddingModel?.let { File(it).name },
        ),
        availableModels = scanLocalModels().map { it.fileName }.sorted(),
    )
}

actual suspend fun saveNodeSettings(values: NodeSettingsValues): NodeSettingsSnapshot {
    NodeConfigStore.save(
        context = SanXianApplication.appContext,
        port = values.port,
        sharedToken = values.sharedToken,
        authEnabled = values.authEnabled,
        maxConcurrency = values.maxConcurrency,
        chatModel = values.chatModel,
        embeddingModel = values.embeddingModel,
    )
    return loadNodeSettings()
}

actual fun generateSharedToken(): String = NodeConfigStore.generateToken()

actual suspend fun currentNodeAccessInfo(): NodeAccessInfo {
    val context = SanXianApplication.appContext
    val port = NodeConfigStore.effectiveConfig(context).port
    return withContext(Dispatchers.IO) { wifiAccessInfo(context, port) }
}

private fun wifiAccessInfo(context: Context, port: Int): NodeAccessInfo {
    val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
        ?: return NodeAccessInfo(null, port, "无法访问网络服务（ConnectivityManager 不可用）")
    val network = cm.activeNetwork
        ?: return NodeAccessInfo(null, port, "当前没有活动网络：请先连接与客户端同一局域网的 Wi-Fi")
    val caps = cm.getNetworkCapabilities(network)
        ?: return NodeAccessInfo(null, port, "无法读取当前网络能力，请稍后刷新")
    if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
        return NodeAccessInfo(
            null,
            port,
            "当前活动网络不是 Wi-Fi（可能是移动数据），局域网内设备无法访问；请连接 Wi-Fi 后刷新",
        )
    }
    val link = cm.getLinkProperties(network)
        ?: return NodeAccessInfo(null, port, "Wi-Fi 已连接但无法读取链路信息，请稍后刷新")
    val ipv4 = link.linkAddresses
        .map { it.address }
        .firstOrNull { it is Inet4Address && !it.isLoopbackAddress && !it.isLinkLocalAddress } as? Inet4Address
        ?: return NodeAccessInfo(null, port, "Wi-Fi 已连接但尚未取得 IPv4 地址（可能仍在获取，请稍后刷新）")
    return NodeAccessInfo(ipv4.hostAddress, port, null)
}

actual fun encodeQrCode(content: String, sizePx: Int): ImageBitmap? = runCatching {
    val hints = mapOf(
        EncodeHintType.MARGIN to 1,
        EncodeHintType.CHARACTER_SET to "UTF-8",
    )
    val matrix = QRCodeWriter().encode(content, BarcodeFormat.QR_CODE, sizePx, sizePx, hints)
    val width = matrix.width
    val height = matrix.height
    val pixels = IntArray(width * height)
    for (y in 0 until height) {
        for (x in 0 until width) {
            pixels[y * width + x] = if (matrix[x, y]) 0xFF000000.toInt() else 0xFFFFFFFF.toInt()
        }
    }
    Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        .apply { setPixels(pixels, 0, width, 0, 0, width, height) }
        .asImageBitmap()
}.getOrNull()

actual fun restartNodeService(): Boolean =
    NodeForegroundService.restart(SanXianApplication.appContext)

actual suspend fun probeNodeHealth(port: Int): Boolean = withContext(Dispatchers.IO) {
    runCatching {
        val conn = URL("http://127.0.0.1:$port/v1/health").openConnection() as HttpURLConnection
        conn.connectTimeout = 1500
        conn.readTimeout = 1500
        conn.requestMethod = "GET"
        try {
            conn.responseCode == HttpURLConnection.HTTP_OK
        } finally {
            conn.disconnect()
        }
    }.getOrDefault(false)
}

actual suspend fun loadNodeChatEndpoint(): NodeChatEndpoint {
    val config = NodeConfigStore.effectiveConfig(SanXianApplication.appContext)
    return NodeChatEndpoint(
        baseUrl = "http://127.0.0.1:${config.port}/v1",
        token = config.sharedToken,
    )
}

actual suspend fun activateNodeChatModel(fileName: String): Boolean {
    val current = loadNodeSettings().values
    saveNodeSettings(current.copy(chatModel = fileName))
    return restartNodeService()
}
