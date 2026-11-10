package com.biyou.sanxian.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.lifecycle.viewmodel.compose.viewModel
import com.biyou.sanxian.ui.theme.DarkColorScheme
import com.biyou.sanxian.ui.theme.LightColorScheme

private enum class Screen { Chat, Models, Settings }

@Composable
fun SanXianApp(darkTheme: Boolean) {
    MaterialTheme(
        colorScheme = if (darkTheme) DarkColorScheme else LightColorScheme,
        typography = com.biyou.sanxian.ui.theme.Typography,
    ) {
        var screen by remember { mutableStateOf(Screen.Chat) }
        // 从设置页返回后递增，触发首页访问信息卡片重新读取端口与 Wi-Fi 地址。
        var accessRefreshKey by remember { mutableStateOf(0) }
        val chatViewModel: ChatViewModel = viewModel()

        when (screen) {
            Screen.Chat -> {
                val uiState by chatViewModel.state.collectAsState()
                ChatScreen(
                    state = uiState,
                    accessRefreshKey = accessRefreshKey,
                    onModelPathChange = chatViewModel::onModelPathChange,
                    onLoadModel = chatViewModel::loadModel,
                    onInputChange = chatViewModel::onInputChange,
                    onSend = chatViewModel::onSend,
                    onStop = chatViewModel::onStop,
                    onOpenModels = { screen = Screen.Models },
                    onOpenSettings = { screen = Screen.Settings },
                )
            }

            Screen.Models -> {
                val modelViewModel: ModelManagerViewModel = viewModel()
                val modelsState by modelViewModel.state.collectAsState()
                ModelManagerScreen(
                    state = modelsState,
                    onBack = {
                        accessRefreshKey++
                        screen = Screen.Chat
                    },
                    onToggleDownload = modelViewModel::toggleDownload,
                    onDelete = modelViewModel::deleteLocal,
                    onUseInChat = { path ->
                        chatViewModel.onModelPathChange(path)
                        screen = Screen.Chat
                    },
                    onImported = modelViewModel::refreshLocal,
                )
            }

            Screen.Settings -> {
                val settingsViewModel: SettingsViewModel = viewModel()
                val settingsState by settingsViewModel.state.collectAsState()
                SettingsScreen(
                    state = settingsState,
                    onBack = {
                        accessRefreshKey++
                        screen = Screen.Chat
                    },
                    onReload = settingsViewModel::reload,
                    onPortChange = settingsViewModel::onPortChange,
                    onTokenChange = settingsViewModel::onTokenChange,
                    onAuthChange = settingsViewModel::onAuthChange,
                    onConcurrencyChange = settingsViewModel::onConcurrencyChange,
                    onChatModelChange = settingsViewModel::onChatModelChange,
                    onEmbeddingModelChange = settingsViewModel::onEmbeddingModelChange,
                    onRegenerateToken = settingsViewModel::regenerateToken,
                    onSave = settingsViewModel::save,
                )
            }
        }
    }
}
