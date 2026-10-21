package com.biyou.sanxian.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.biyou.sanxian.llm.ChatMessage
import com.biyou.sanxian.llm.ChatSession
import com.biyou.sanxian.llm.LlmConfig
import com.biyou.sanxian.llm.LlmEngine
import com.biyou.sanxian.llm.LlmEngineProvider
import com.biyou.sanxian.llm.LlmError
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * 对话界面 ViewModel（MVI 雏形）。
 * Task #3 起由 Android JNI 引擎驱动真实本地推理：用户先指定 GGUF 模型路径并加载，
 * 之后输入消息即可看到逐字流式回复。
 */
enum class ModelStatus { Idle, Loading, Loaded, Error }

data class ChatUiState(
    val messages: List<ChatMessage> = emptyList(),
    val inputText: String = "",
    val isGenerating: Boolean = false,
    val modelPath: String = "",
    val modelStatus: ModelStatus = ModelStatus.Idle,
    val modelInfo: String? = null,
    val modelError: String? = null,
)

class ChatViewModel : ViewModel() {

    private val engine: LlmEngine = LlmEngineProvider.engine
    private val _state = MutableStateFlow(ChatUiState())
    val state: StateFlow<ChatUiState> = _state.asStateFlow()

    private var generatingIndex = -1

    fun onModelPathChange(text: String) {
        _state.update { it.copy(modelPath = text) }
    }

    /** 加载指定路径的 GGUF 模型（在 IO 线程同步执行，完成后切回主线程更新状态）。 */
    fun loadModel() {
        val path = _state.value.modelPath.trim()
        if (path.isEmpty() || _state.value.modelStatus == ModelStatus.Loading) return
        _state.update { it.copy(modelStatus = ModelStatus.Loading, modelError = null) }
        viewModelScope.launch(Dispatchers.IO) {
            try {
                engine.loadModel(LlmConfig(modelPath = path))
                val info = engine.getModelInfo()
                _state.update {
                    it.copy(
                        modelStatus = ModelStatus.Loaded,
                        modelInfo = info?.let { i -> "${i.name} · ${i.backend}" },
                    )
                }
            } catch (e: LlmError) {
                _state.update { it.copy(modelStatus = ModelStatus.Error, modelError = e.message) }
            }
        }
    }

    fun onInputChange(text: String) {
        _state.update { it.copy(inputText = text) }
    }

    fun onSend() {
        val text = _state.value.inputText.trim()
        if (text.isEmpty() || _state.value.isGenerating) return
        if (_state.value.modelStatus != ModelStatus.Loaded) {
            appendError("请先在上方加载 GGUF 模型")
            return
        }

        val userMsg = ChatMessage.user(text)
        val assistantMsg = ChatMessage.assistant("")
        val list = _state.value.messages + userMsg + assistantMsg
        generatingIndex = list.lastIndex
        _state.update { it.copy(messages = list, inputText = "", isGenerating = true) }

        viewModelScope.launch {
            val history = _state.value.messages.dropLast(1)
            val session = ChatSession(
                messages = history,
                systemPrompt = "你是一个运行在手机上的本地离线 AI 助手，回答简洁准确。",
            )
            try {
                engine.generate(session, text).collect { chunk ->
                    _state.update { s ->
                        val msgs = s.messages.toMutableList()
                        if (generatingIndex in msgs.indices) {
                            val cur = msgs[generatingIndex]
                            msgs[generatingIndex] = cur.copy(content = cur.content + chunk)
                        }
                        s.copy(messages = msgs)
                    }
                }
            } catch (e: LlmError) {
                appendError(e.message ?: "unknown error")
            } finally {
                _state.update { it.copy(isGenerating = false) }
                generatingIndex = -1
            }
        }
    }

    fun onStop() {
        engine.stopGeneration()
        _state.update { it.copy(isGenerating = false) }
        generatingIndex = -1
    }

    private fun appendError(msg: String) {
        _state.update { s ->
            val msgs = s.messages.toMutableList()
            if (generatingIndex in msgs.indices) {
                val cur = msgs[generatingIndex]
                msgs[generatingIndex] = cur.copy(content = cur.content + "\n[错误] $msg")
            } else {
                msgs.add(ChatMessage.assistant("[错误] $msg"))
            }
            s.copy(messages = msgs)
        }
    }

    override fun onCleared() {
        super.onCleared()
        // 引擎为进程级单例（LlmEngineProvider），不随 ViewModel 销毁；
        // 模型卸载由显式入口调用 engine.close()，避免误释放共享模型。
    }
}
