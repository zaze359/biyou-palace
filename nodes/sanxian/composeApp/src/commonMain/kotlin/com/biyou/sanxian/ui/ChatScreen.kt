package com.biyou.sanxian.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.biyou.sanxian.ui.components.MessageBubble
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(
    state: ChatUiState,
    accessRefreshKey: Int,
    onInputChange: (String) -> Unit,
    onSend: () -> Unit,
    onStop: () -> Unit,
    onOpenModels: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()

    LaunchedEffect(state.messages.size, state.messages.lastOrNull()?.content) {
        if (state.messages.isNotEmpty()) {
            scope.launch { listState.animateScrollToItem(state.messages.lastIndex) }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("小手机 · 离线对话") },
                actions = {
                    IconButton(onClick = onOpenModels) {
                        Icon(Icons.Filled.Folder, contentDescription = "模型管理")
                    }
                    IconButton(onClick = onOpenSettings) {
                        Icon(Icons.Filled.Settings, contentDescription = "节点设置")
                    }
                },
            )
        },
        bottomBar = {
            ChatInputBar(
                text = state.inputText,
                isGenerating = state.isGenerating,
                onTextChange = onInputChange,
                onSend = onSend,
                onStop = onStop,
            )
        },
    ) { padding ->
        Column(modifier = Modifier.fillMaxSize().padding(padding)) {
            NodeAccessCard(
                refreshKey = accessRefreshKey,
                modifier = Modifier.padding(horizontal = 8.dp, vertical = 8.dp),
            )
            NodeModelBar(
                state = state,
                onOpenSettings = onOpenSettings,
            )
            if (state.messages.isEmpty()) {
                Box(modifier = Modifier.fillMaxSize().weight(1f), contentAlignment = Alignment.Center) {
                    Text(
                        text = "消息经由本机节点服务生成",
                        style = MaterialTheme.typography.bodyLarge,
                        color = MaterialTheme.colorScheme.outline,
                    )
                }
            } else {
                LazyColumn(
                    state = listState,
                    modifier = Modifier.fillMaxSize().weight(1f),
                ) {
                    items(state.messages, key = { it.id }) { msg ->
                        MessageBubble(msg)
                    }
                }
            }
        }
    }
}

/**
 * 节点模型状态条（只读）。
 *
 * 聊天已改走节点 HTTP 端点，页面不再提供「加载/切换模型」入口：
 * 换模型必须到「节点设置」保存并重启前台服务，避免关掉正在对外服务的引擎句柄。
 */
@Composable
private fun NodeModelBar(
    state: ChatUiState,
    onOpenSettings: () -> Unit,
) {
    Surface(tonalElevation = 2.dp) {
        Column(modifier = Modifier.fillMaxWidth().padding(8.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(modifier = Modifier.weight(1f).padding(start = 12.dp, top = 4.dp, bottom = 4.dp)) {
                    Text(
                        text = when {
                            state.checkingNode -> "正在读取节点模型状态…"
                            state.nodeModel != null -> "节点模型：${state.nodeModel}"
                            else -> "节点模型：未加载"
                        },
                        style = MaterialTheme.typography.bodyMedium,
                        color = if (state.nodeModel != null) {
                            MaterialTheme.colorScheme.primary
                        } else {
                            MaterialTheme.colorScheme.error
                        },
                    )
                    Text(
                        text = if (state.endpoint.isBlank()) "本机节点端点" else state.endpoint,
                        style = MaterialTheme.typography.labelSmall,
                        fontFamily = FontFamily.Monospace,
                        color = MaterialTheme.colorScheme.outline,
                    )
                }
                TextButton(onClick = onOpenSettings) {
                    Text(if (state.nodeModel == null) "去设置" else "切换模型")
                }
            }
            state.nodeError?.takeIf { !state.checkingNode }?.let {
                Text(
                    text = it,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(start = 12.dp, bottom = 4.dp),
                )
            }
        }
    }
}

@Composable
private fun ChatInputBar(
    text: String,
    isGenerating: Boolean,
    onTextChange: (String) -> Unit,
    onSend: () -> Unit,
    onStop: () -> Unit,
) {
    Surface(tonalElevation = 4.dp) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .imePadding()
                .padding(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            BasicTextField(
                value = text,
                onValueChange = onTextChange,
                modifier = Modifier
                    .weight(1f)
                    .padding(12.dp),
                decorationBox = { inner ->
                    if (text.isEmpty()) {
                        Text("输入消息…", color = MaterialTheme.colorScheme.outline)
                    } else {
                        inner()
                    }
                },
            )
            if (isGenerating) {
                IconButton(onClick = onStop) {
                    Icon(Icons.Filled.Stop, contentDescription = "停止生成")
                }
            } else {
                IconButton(onClick = onSend, enabled = text.isNotBlank()) {
                    Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "发送")
                }
            }
        }
    }
}
