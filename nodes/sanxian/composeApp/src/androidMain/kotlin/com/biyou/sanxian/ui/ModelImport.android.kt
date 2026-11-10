package com.biyou.sanxian.ui

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.FileDownload
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileOutputStream

/**
 * SAF 导入实现：`OpenDocument` 选 GGUF → 复制到应用私有模型目录。
 * 复制在 IO 线程进行并按字节进度回报；不申请任何存储权限。
 */
@Composable
actual fun ImportModelButton(onImported: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val status = remember { MutableStateFlow<ImportStatus?>(null) }
    val current by status.collectAsState()

    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        if (uri == null) return@rememberLauncherForActivityResult
        scope.launch {
            status.value = ImportStatus(0f, "导入中…")
            runCatching {
                copyGgufIntoModelsDir(context, uri) { pct ->
                    status.value = ImportStatus(pct, "导入中… ${(pct * 100).toInt()}%")
                }
            }.onSuccess { file ->
                status.value = ImportStatus(1f, "已导入 ${file.name}（${formatBytes(file.length())}）")
                onImported()
            }.onFailure { e ->
                status.value = ImportStatus(-1f, "导入失败：${e.message ?: "未知错误"}")
            }
        }
    }

    Column(modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
        Button(onClick = { picker.launch(arrayOf("*/*")) }) {
            Icon(Icons.Filled.FileDownload, contentDescription = null)
            Text("  导入本地 GGUF")
        }
        current?.let { s ->
            if (s.fraction in 0f..1f) {
                LinearProgressIndicator(
                    progress = { s.fraction },
                    modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                )
            }
            Text(
                text = s.message,
                style = MaterialTheme.typography.labelSmall,
                color = if (s.fraction < 0f) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.outline,
                modifier = Modifier.padding(top = 4.dp),
            )
        }
        Text(
            text = "从文件选择器挑 .gguf，复制到私有目录 ${getModelsDirectory()}",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.outline,
        )
    }
}

private data class ImportStatus(val fraction: Float, val message: String)

private suspend fun copyGgufIntoModelsDir(
    context: Context,
    uri: Uri,
    onProgress: (Float) -> Unit,
): File = withContext(Dispatchers.IO) {
    val resolver = context.contentResolver
    val rawName = queryColumn(context, uri, OpenableColumns.DISPLAY_NAME) ?: "imported-${System.currentTimeMillis()}.gguf"
    val name = File(rawName).name
    require(name.endsWith(".gguf", ignoreCase = true)) { "仅支持 .gguf 文件（当前：$name）" }

    val dir = File(getModelsDirectory()).apply { mkdirs() }
    val dest = File(dir, name)
    val tmp = File(dir, "$name.part")
    val total = queryColumn(context, uri, OpenableColumns.SIZE)?.toLongOrNull() ?: -1L

    resolver.openInputStream(uri)?.use { input ->
        FileOutputStream(tmp).use { out ->
            val buf = ByteArray(1 shl 20)
            var copied = 0L
            var lastPct = -1
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                out.write(buf, 0, n)
                copied += n
                if (total > 0) {
                    val pct = (copied * 100 / total).toInt()
                    if (pct != lastPct) {
                        lastPct = pct
                        onProgress(pct / 100f)
                    }
                }
            }
        }
    } ?: error("无法读取所选文件")

    if (dest.exists() && !dest.delete()) error("无法覆盖同名文件：${dest.name}")
    if (!tmp.renameTo(dest)) error("写入目标文件失败：${dest.absolutePath}")
    onProgress(1f)
    dest
}

private fun queryColumn(context: Context, uri: Uri, column: String): String? =
    context.contentResolver.query(uri, arrayOf(column), null, null, null)?.use { c ->
        if (c.moveToFirst() && !c.isNull(0)) c.getString(0) else null
    }
