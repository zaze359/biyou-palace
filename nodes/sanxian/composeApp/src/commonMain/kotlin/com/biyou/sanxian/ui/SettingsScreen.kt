package com.biyou.sanxian.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

/**
 * 设备端设置页：修改运行配置（保存后重启前台服务生效，无需重打包）。
 * 读取优先级：DataStore 用户配置 > assets 默认值。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    state: SettingsUiState,
    onBack: () -> Unit,
    onReload: () -> Unit,
    onPortChange: (String) -> Unit,
    onTokenChange: (String) -> Unit,
    onAuthChange: (Boolean) -> Unit,
    onConcurrencyChange: (String) -> Unit,
    onChatModelChange: (String) -> Unit,
    onEmbeddingModelChange: (String) -> Unit,
    onRegenerateToken: () -> Unit,
    onSave: () -> Unit,
) {
    val clipboard = LocalClipboardManager.current

    // 每次进入设置页都重新读取生效配置，避免显示上次保存后的过期状态。
    androidx.compose.runtime.LaunchedEffect(Unit) { onReload() }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("节点设置") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Filled.ArrowBack, contentDescription = "返回")
                    }
                },
            )
        },
    ) { padding ->
        if (state.loading) {
            Box(modifier = Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
            return@Scaffold
        }

        LazyColumn(
            modifier = Modifier.fillMaxSize().padding(padding).padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(vertical = 12.dp),
        ) {
            item {
                Text(
                    "修改后点「保存并重启服务」生效，不需要重新打包。重启期间连接会短暂中断。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }

            item { SectionTitle("服务") }

            item {
                OutlinedTextField(
                    value = state.portText,
                    onValueChange = onPortChange,
                    label = { Text("端口") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            item {
                OutlinedTextField(
                    value = state.concurrencyText,
                    onValueChange = onConcurrencyChange,
                    label = { Text("最大并发（1~8）") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            item {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(modifier = Modifier.weight(1f)) {
                        Text("Bearer 鉴权", style = MaterialTheme.typography.bodyLarge)
                        Text(
                            "开启后所有端点（含 /v1/health）都要求 token",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.outline,
                        )
                    }
                    Switch(checked = state.authEnabled, onCheckedChange = onAuthChange)
                }
            }

            item {
                OutlinedTextField(
                    value = state.token,
                    onValueChange = onTokenChange,
                    label = { Text("shared_token") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            item {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = {
                        clipboard.setText(AnnotatedString(state.token))
                    }) {
                        Icon(Icons.Filled.ContentCopy, contentDescription = null)
                        Text("复制 token", modifier = Modifier.padding(start = 6.dp))
                    }
                    OutlinedButton(onClick = onRegenerateToken) {
                        Icon(Icons.Filled.Refresh, contentDescription = null)
                        Text("随机生成", modifier = Modifier.padding(start = 6.dp))
                    }
                }
            }

            item { SectionTitle("模型") }

            item {
                ModelSelector(
                    label = "chat 模型",
                    selected = state.chatModel,
                    options = state.availableModels,
                    allowNone = false,
                    onSelect = onChatModelChange,
                )
            }

            item {
                ModelSelector(
                    label = "embedding 模型",
                    selected = state.embeddingModel,
                    options = state.availableModels,
                    allowNone = true,
                    onSelect = onEmbeddingModelChange,
                )
            }

            if (state.availableModels.isEmpty()) {
                item {
                    Text(
                        "模型目录中还没有 GGUF，请先到「模型管理」下载或导入。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.error,
                    )
                }
            }

            item {
                Button(
                    onClick = onSave,
                    enabled = !state.saving,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    if (state.saving) {
                        CircularProgressIndicator(modifier = Modifier.padding(end = 8.dp))
                    }
                    Text(if (state.saving) "保存中…" else "保存并重启服务")
                }
            }

            state.statusMessage?.let { message ->
                item {
                    Surface(
                        tonalElevation = 2.dp,
                        shape = RoundedCornerShape(12.dp),
                        color = MaterialTheme.colorScheme.secondaryContainer,
                    ) {
                        Text(
                            message,
                            modifier = Modifier.fillMaxWidth().padding(12.dp),
                            style = MaterialTheme.typography.bodyMedium,
                        )
                    }
                }
            }
            state.errorMessage?.let { message ->
                item {
                    Surface(
                        tonalElevation = 2.dp,
                        shape = RoundedCornerShape(12.dp),
                        color = MaterialTheme.colorScheme.errorContainer,
                    ) {
                        Text(
                            message,
                            modifier = Modifier.fillMaxWidth().padding(12.dp),
                            style = MaterialTheme.typography.bodyMedium,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun SectionTitle(title: String) {
    Text(
        text = title,
        style = MaterialTheme.typography.titleSmall,
        color = MaterialTheme.colorScheme.primary,
        modifier = Modifier.padding(top = 4.dp),
    )
}

@Composable
private fun ModelSelector(
    label: String,
    selected: String,
    options: List<String>,
    allowNone: Boolean,
    onSelect: (String) -> Unit,
) {
    var expanded by remember { mutableStateOf(false) }
    val allOptions = remember(options, selected) {
        (options + selected).filter { it.isNotBlank() }.distinct().sorted()
    }
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(label, style = MaterialTheme.typography.bodyLarge)
        Box {
            OutlinedButton(
                onClick = { expanded = true },
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(
                    text = selected.ifBlank { if (allowNone) "不启用" else "未选择" },
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
                if (allowNone) {
                    DropdownMenuItem(
                        text = { Text("不启用") },
                        onClick = {
                            expanded = false
                            onSelect("")
                        },
                    )
                }
                allOptions.forEach { name ->
                    DropdownMenuItem(
                        text = { Text(name, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                        onClick = {
                            expanded = false
                            onSelect(name)
                        },
                    )
                }
            }
        }
    }
}
