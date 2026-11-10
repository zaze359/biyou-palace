package com.biyou.sanxian.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

/**
 * 首页访问信息卡片：`http://<Wi-Fi IP>:<端口>/v1` + 同内容二维码 + 当前 token（默认脱敏）。
 *
 * IP 取当前 Wi-Fi 链路的 IPv4；取不到时展示具体原因（未连 Wi-Fi / 只有移动数据 / 尚无 IPv4）。
 * @param refreshKey 从设置页返回时递增，触发重新读取端口与地址
 */
@Composable
fun NodeAccessCard(refreshKey: Int, modifier: Modifier = Modifier) {
    val clipboard = LocalClipboardManager.current
    var info by remember { mutableStateOf<NodeAccessInfo?>(null) }
    var token by remember { mutableStateOf("") }
    var showToken by remember { mutableStateOf(false) }
    var qr by remember { mutableStateOf<ImageBitmap?>(null) }
    var copiedHint by remember { mutableStateOf<String?>(null) }
    var manualRefresh by remember { mutableIntStateOf(0) }

    LaunchedEffect(refreshKey, manualRefresh) {
        val snapshot = runCatching { loadNodeSettings() }.getOrNull()
        token = snapshot?.values?.sharedToken.orEmpty()
        val access = runCatching { currentNodeAccessInfo() }.getOrNull()
        info = access
        qr = access?.baseUrl?.let { url ->
            withContext(Dispatchers.Default) { encodeQrCode(url, 512) }
        }
    }
    LaunchedEffect(copiedHint) {
        if (copiedHint != null) {
            delay(1500)
            copiedHint = null
        }
    }

    Surface(
        modifier = modifier.fillMaxWidth(),
        tonalElevation = 2.dp,
        shape = RoundedCornerShape(12.dp),
    ) {
        Column(
            modifier = Modifier.fillMaxWidth().padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    "节点访问",
                    style = MaterialTheme.typography.titleSmall,
                    color = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.weight(1f),
                )
                copiedHint?.let {
                    Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.primary)
                }
                IconButton(onClick = { manualRefresh++ }) {
                    Icon(Icons.Filled.Refresh, contentDescription = "刷新访问信息")
                }
            }

            val url = info?.baseUrl
            if (url != null) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        text = url,
                        style = MaterialTheme.typography.bodyMedium,
                        fontFamily = FontFamily.Monospace,
                        modifier = Modifier.weight(1f),
                    )
                    IconButton(onClick = {
                        clipboard.setText(AnnotatedString(url))
                        copiedHint = "地址已复制"
                    }) {
                        Icon(Icons.Filled.ContentCopy, contentDescription = "复制访问地址")
                    }
                }
                qr?.let { bitmap ->
                    Image(
                        bitmap = bitmap,
                        contentDescription = "访问地址二维码",
                        modifier = Modifier.size(132.dp).align(Alignment.CenterHorizontally),
                    )
                    Text(
                        "扫码得到上面的地址（不含 token）",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.outline,
                        modifier = Modifier.align(Alignment.CenterHorizontally),
                    )
                }
            } else {
                Text(
                    text = info?.reason ?: "正在读取 Wi-Fi 地址…",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }

            HorizontalDivider(modifier = Modifier.padding(vertical = 4.dp))

            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(modifier = Modifier.weight(1f)) {
                    Text("Token", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.outline)
                    Text(
                        text = if (showToken) token else maskToken(token),
                        style = MaterialTheme.typography.bodyMedium,
                        fontFamily = FontFamily.Monospace,
                        modifier = Modifier.clickable { showToken = !showToken },
                    )
                }
                TextButton(onClick = { showToken = !showToken }) {
                    Text(if (showToken) "隐藏" else "显示")
                }
                IconButton(
                    onClick = {
                        clipboard.setText(AnnotatedString(token))
                        copiedHint = "token 已复制"
                    },
                    enabled = token.isNotBlank(),
                ) {
                    Icon(Icons.Filled.ContentCopy, contentDescription = "复制 token")
                }
            }
            Text(
                if (showToken) "点击 token 或「隐藏」可再次脱敏" else "点击 token 或「显示」可查看完整值",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }
    }
}

/** 默认脱敏：固定 8 个圆点 + 末 4 位，足以区分 token 又不泄漏完整值。 */
private fun maskToken(token: String): String = when {
    token.isBlank() -> "（未设置）"
    token.length <= 4 -> "••••"
    else -> "••••••••" + token.takeLast(4)
}
