package com.biyou.sanxian.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * 设置页状态：字段用字符串承接用户输入（数字允许中间态），保存时统一校验。
 * @param savedValues 上一次生效的配置，用于判断端口是否变化以提示「连接会中断」
 */
data class SettingsUiState(
    val loading: Boolean = true,
    val portText: String = "",
    val token: String = "",
    val authEnabled: Boolean = false,
    val concurrencyText: String = "",
    val chatModel: String = "",
    val embeddingModel: String = "",
    val availableModels: List<String> = emptyList(),
    val savedValues: NodeSettingsValues? = null,
    val saving: Boolean = false,
    val statusMessage: String? = null,
    val errorMessage: String? = null,
)

class SettingsViewModel : ViewModel() {

    private val _state = MutableStateFlow(SettingsUiState())
    val state: StateFlow<SettingsUiState> = _state.asStateFlow()

    fun reload() {
        viewModelScope.launch {
            val snapshot = runCatching { loadNodeSettings() }.getOrNull()
            if (snapshot == null) {
                _state.update { it.copy(loading = false, errorMessage = "读取设备配置失败") }
                return@launch
            }
            val v = snapshot.values
            _state.update {
                it.copy(
                    loading = false,
                    portText = v.port.toString(),
                    token = v.sharedToken,
                    authEnabled = v.authEnabled,
                    concurrencyText = v.maxConcurrency.toString(),
                    chatModel = v.chatModel.orEmpty(),
                    embeddingModel = v.embeddingModel.orEmpty(),
                    availableModels = snapshot.availableModels,
                    savedValues = v,
                    errorMessage = null,
                )
            }
        }
    }

    fun onPortChange(text: String) = _state.update { it.copy(portText = text.filter(Char::isDigit)) }
    fun onTokenChange(text: String) = _state.update { it.copy(token = text.trim()) }
    fun onAuthChange(enabled: Boolean) = _state.update { it.copy(authEnabled = enabled) }
    fun onConcurrencyChange(text: String) =
        _state.update { it.copy(concurrencyText = text.filter(Char::isDigit)) }

    fun onChatModelChange(name: String) = _state.update { it.copy(chatModel = name) }
    fun onEmbeddingModelChange(name: String) =
        _state.update { it.copy(embeddingModel = name) }

    fun regenerateToken() = _state.update { it.copy(token = generateSharedToken()) }

    fun save() {
        val s = _state.value
        if (s.saving) return
        val port = s.portText.toIntOrNull()
        if (port == null || port !in 1024..65535) {
            _state.update { it.copy(errorMessage = "端口需为 1024~65535 的整数") }
            return
        }
        val concurrency = s.concurrencyText.toIntOrNull()
        if (concurrency == null || concurrency !in 1..8) {
            _state.update { it.copy(errorMessage = "并发上限需为 1~8 的整数") }
            return
        }
        if (s.token.isBlank()) {
            _state.update { it.copy(errorMessage = "shared_token 不能为空，可点「随机生成」") }
            return
        }
        if (s.chatModel.isBlank()) {
            _state.update { it.copy(errorMessage = "请选择 chat 模型（需先在「模型管理」导入 GGUF）") }
            return
        }

        val values = NodeSettingsValues(
            port = port,
            sharedToken = s.token,
            authEnabled = s.authEnabled,
            maxConcurrency = concurrency,
            chatModel = s.chatModel,
            embeddingModel = s.embeddingModel.ifBlank { null },
        )
        val before = s.savedValues

        viewModelScope.launch {
            _state.update { it.copy(saving = true, errorMessage = null, statusMessage = "正在保存并重启服务…") }
            val saved = runCatching { saveNodeSettings(values) }.getOrElse { e ->
                _state.update {
                    it.copy(saving = false, statusMessage = null, errorMessage = "保存失败：${e.message}")
                }
                return@launch
            }
            val restarted = restartNodeService()
            if (!restarted) {
                _state.update {
                    it.copy(saving = false, statusMessage = null, errorMessage = "配置已保存，但服务重启请求被系统拒绝，请回到首页重试")
                }
                return@launch
            }

            // 服务重启包含重新加载模型，可能耗时数秒；逐秒探活，最长 30s。
            var healthy = false
            for (i in 1..30) {
                delay(1000)
                if (probeNodeHealth(port)) {
                    healthy = true
                    break
                }
            }

            val portChanged = before != null && before.port != port
            val message = buildString {
                append("配置已保存，服务已重启。")
                if (portChanged) {
                    append("端口由 ${before?.port} 改为 $port，旧端口的连接已中断，客户端请改用新地址。")
                }
                if (healthy) append("新端口 $port 探活成功。")
            }
            _state.update {
                it.copy(
                    saving = false,
                    savedValues = saved.values,
                    availableModels = saved.availableModels,
                    statusMessage = if (healthy) message else null,
                    errorMessage = if (healthy) null else
                        "配置已保存并重启，但 30 秒内 $port 端口探活失败；请返回首页确认服务状态或重新保存。",
                )
            }
        }
    }
}
