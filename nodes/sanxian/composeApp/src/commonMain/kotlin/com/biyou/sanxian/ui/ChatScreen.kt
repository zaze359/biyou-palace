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
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.biyou.sanxian.ui.components.MessageBubble
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(
    state: ChatUiState,
    accessRefreshKey: Int,
    onModelPathChange: (String) -> Unit,
    onLoadModel: () -> Unit,
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
            ModelLoadBar(
                modelPath = state.modelPath,
                status = state.modelStatus,
                modelInfo = state.modelInfo,
                modelError = state.modelError,
                onPathChange = onModelPathChange,
                onLoad = onLoadModel,
            )
            if (state.messages.isEmpty()) {
                Box(modifier = Modifier.fillMaxSize().weight(1f), contentAlignment = Alignment.Center) {
                    Text(
                        text = "加载模型后，即可开始离线对话",
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

@Composable
private fun ModelLoadBar(
    modelPath: String,
    status: ModelStatus,
    modelInfo: String?,
    modelError: String?,
    onPathChange: (String) -> Unit,
    onLoad: () -> Unit,
) {
    Surface(tonalElevation = 2.dp) {
        Column(modifier = Modifier.fillMaxWidth().padding(8.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                BasicTextField(
                    value = modelPath,
                    onValueChange = onPathChange,
                    modifier = Modifier.weight(1f).padding(12.dp),
                    decorationBox = { inner ->
                        if (modelPath.isEmpty()) {
                            Text("GGUF 模型文件路径，如 /sdcard/Download/qwen2.5-1.5b.gguf", color = MaterialTheme.colorScheme.outline)
                        } else {
                            inner()
                        }
                    },
                )
                Button(
                    onClick = onLoad,
                    enabled = modelPath.isNotBlank() && status != ModelStatus.Loading,
                ) {
                    if (status == ModelStatus.Loading) {
                        CircularProgressIndicator(modifier = Modifier.padding(end = 6.dp))
                    }
                    Text(if (status == ModelStatus.Loaded) "重载" else "加载")
                }
            }
            when {
                status == ModelStatus.Loaded && modelInfo != null ->
                    Text("已加载：$modelInfo", color = MaterialTheme.colorScheme.primary, modifier = Modifier.padding(start = 12.dp, bottom = 4.dp))
                status == ModelStatus.Error && modelError != null ->
                    Text("加载失败：$modelError", color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(start = 12.dp, bottom = 4.dp))
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
