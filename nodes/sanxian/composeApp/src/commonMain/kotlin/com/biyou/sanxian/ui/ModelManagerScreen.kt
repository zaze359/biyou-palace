package com.biyou.sanxian.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.ArrowDownward
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.biyou.sanxian.llm.DownloadState

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ModelManagerScreen(
    state: ModelManagerUiState,
    onBack: () -> Unit,
    onToggleDownload: (CatalogEntry) -> Unit,
    onDelete: (LocalModel) -> Unit,
    onUseInChat: (String) -> Unit,
    onImported: () -> Unit,
) {
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("模型管理") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Filled.ArrowBack, contentDescription = "返回对话")
                    }
                },
            )
        },
    ) { padding ->
        LazyColumn(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(horizontal = 12.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            item { SectionHeader("已下载模型") }
            item { ImportModelButton(onImported = onImported) }
            state.statusMessage?.let { message ->
                item {
                    Text(
                        text = message,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.primary,
                    )
                }
            }
            if (state.localModels.isEmpty()) {
                item {
                    Text(
                        "暂无已下载模型。从下方目录下载，或手动把 GGUF 放到：\n${state.modelsDir}",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.outline,
                    )
                }
            } else {
                items(state.localModels, key = { it.path }) { model ->
                    LocalModelRow(
                        model = model,
                        switching = state.switching,
                        onUseInChat = { onUseInChat(model.fileName) },
                        onDelete = { onDelete(model) },
                    )
                }
            }

            item { SectionHeader("可下载模型（HuggingFace GGUF）") }
            items(state.catalog, key = { it.fileName }) { entry ->
                CatalogRow(
                    entry = entry,
                    downloadState = state.downloads[entry.fileName],
                    onToggle = { onToggleDownload(entry) },
                )
            }

            item { StorageFooter(state) }
        }
    }
}

@Composable
private fun SectionHeader(title: String) {
    Text(
        text = title,
        style = MaterialTheme.typography.titleSmall,
        color = MaterialTheme.colorScheme.primary,
        modifier = Modifier.padding(top = 4.dp),
    )
}

@Composable
private fun LocalModelRow(
    model: LocalModel,
    switching: Boolean,
    onUseInChat: () -> Unit,
    onDelete: () -> Unit,
) {
    Surface(tonalElevation = 2.dp, shape = RoundedCornerShape(12.dp)) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(model.fileName, style = MaterialTheme.typography.bodyMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(formatBytes(model.sizeBytes), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline)
            }
            Button(onClick = onUseInChat, enabled = !switching) {
                Icon(Icons.Filled.PlayArrow, contentDescription = null)
                Text("设为节点模型")
            }
            IconButton(onClick = onDelete) {
                Icon(Icons.Filled.Delete, contentDescription = "删除", tint = MaterialTheme.colorScheme.error)
            }
        }
    }
}

@Composable
private fun CatalogRow(
    entry: CatalogEntry,
    downloadState: DownloadState?,
    onToggle: () -> Unit,
) {
    Surface(tonalElevation = 1.dp, shape = RoundedCornerShape(12.dp)) {
        Column(modifier = Modifier.fillMaxWidth().padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(entry.displayName, style = MaterialTheme.typography.bodyMedium)
                    Text(
                        entry.sizeBytes?.let { formatBytes(it) } ?: "大小未知",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                    )
                }
                Button(
                    onClick = onToggle,
                    enabled = downloadState !is DownloadState.Progress,
                ) {
                    when (downloadState) {
                        is DownloadState.Progress -> {
                            CircularProgressIndicator(modifier = Modifier.padding(end = 6.dp))
                            Text("取消")
                        }
                        is DownloadState.Error -> Text("重试")
                        is DownloadState.Completed -> Text("下载")
                        else -> {
                            Icon(Icons.Filled.ArrowDownward, contentDescription = null)
                            Text("下载")
                        }
                    }
                }
            }
            if (downloadState is DownloadState.Progress) {
                val total = downloadState.totalBytes
                if (total > 0) {
                    val frac = (downloadState.downloadedBytes.toFloat() / total).coerceIn(0f, 1f)
                    LinearProgressIndicator(progress = { frac }, modifier = Modifier.fillMaxWidth().padding(top = 8.dp))
                    Text(
                        text = "${(frac * 100).toInt()}% · ${formatBytes(downloadState.bytesPerSecond)}/s",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                        modifier = Modifier.padding(top = 2.dp),
                    )
                } else {
                    LinearProgressIndicator(modifier = Modifier.fillMaxWidth().padding(top = 8.dp))
                    Text(
                        text = "${formatBytes(downloadState.downloadedBytes)} 已下载（总大小未知）",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                        modifier = Modifier.padding(top = 2.dp),
                    )
                }
            } else if (downloadState is DownloadState.Error) {
                Text(
                    text = "下载失败：${downloadState.message}",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }
        }
    }
}

@Composable
private fun StorageFooter(state: ModelManagerUiState) {
    val used = (state.storage.totalBytes - state.storage.freeBytes).coerceAtLeast(0)
    Surface(tonalElevation = 1.dp, shape = RoundedCornerShape(12.dp)) {
        Column(modifier = Modifier.fillMaxWidth().padding(12.dp)) {
            Text("存储占用", style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.primary)
            Text(
                "已用 ${formatBytes(used)} / 可用 ${formatBytes(state.storage.freeBytes)}",
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.padding(top = 4.dp),
            )
            Text(
                "模型目录：${state.modelsDir}",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
                modifier = Modifier.padding(top = 2.dp),
            )
        }
    }
}

/** 人类可读字节数（如 1.2 GB）。 */
fun formatBytes(bytes: Long): String {
    if (bytes <= 0) return "0 B"
    val units = listOf("B", "KB", "MB", "GB", "TB")
    var value = bytes.toDouble()
    var i = 0
    while (value >= 1024 && i < units.lastIndex) {
        value /= 1024
        i++
    }
    return "%.1f %s".format(value, units[i])
}
