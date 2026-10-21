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

private enum class Screen { Chat, Models }

@Composable
fun SanXianApp(darkTheme: Boolean) {
    MaterialTheme(
        colorScheme = if (darkTheme) DarkColorScheme else LightColorScheme,
        typography = com.biyou.sanxian.ui.theme.Typography,
    ) {
        var screen by remember { mutableStateOf(Screen.Chat) }
        val chatViewModel: ChatViewModel = viewModel()

        when (screen) {
            Screen.Chat -> {
                val uiState by chatViewModel.state.collectAsState()
                ChatScreen(
                    state = uiState,
                    onModelPathChange = chatViewModel::onModelPathChange,
                    onLoadModel = chatViewModel::loadModel,
                    onInputChange = chatViewModel::onInputChange,
                    onSend = chatViewModel::onSend,
                    onStop = chatViewModel::onStop,
                    onOpenModels = { screen = Screen.Models },
                )
            }

            Screen.Models -> {
                val modelViewModel: ModelManagerViewModel = viewModel()
                val modelsState by modelViewModel.state.collectAsState()
                ModelManagerScreen(
                    state = modelsState,
                    onBack = { screen = Screen.Chat },
                    onToggleDownload = modelViewModel::toggleDownload,
                    onDelete = modelViewModel::deleteLocal,
                    onUseInChat = { path ->
                        chatViewModel.onModelPathChange(path)
                        screen = Screen.Chat
                    },
                )
            }
        }
    }
}
