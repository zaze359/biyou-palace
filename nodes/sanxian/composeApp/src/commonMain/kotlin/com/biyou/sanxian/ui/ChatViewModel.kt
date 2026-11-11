package com.biyou.sanxian.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.biyou.sanxian.contract.ChatMessage as ContractChatMessage
import com.biyou.sanxian.llm.ChatMessage
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * 对话界面 ViewModel。
 *
 * 聊天**只走节点自身的 HTTP 端点**（`http://127.0.0.1:<端口>/v1/chat/completions`，契约原则 2），
 * 不再直连进程内引擎单例——因此聊天页无法、也不允许切换正在对外服务的模型：
 * 模型状态只从 `/v1/health` 只读获取，切换模型需到「节点设置」保存并由前台服务重启加载。
 */
data class ChatUiState(
    val messages: List<ChatMessage> = emptyList(),
    val inputText: String = "",
    val isGenerating: Boolean = false,
    val checkingNode: Boolean = true,
    /** 节点当前已加载的 chat 模型（来自 /v1/health）；null 表示无可用模型 */
    val nodeModel: String? = null,
    val nodeStatus: String? = null,
    /** 本机端点，如 `http://127.0.0.1:4011/v1`，仅用于展示 */
    val endpoint: String = "",
    val nodeError: String? = null,
)

class ChatViewModel(
    private val client: NodeChatClient = KtorNodeChatClient(),
    private val endpointProvider: suspend () -> NodeChatEndpoint = { loadNodeChatEndpoint() },
) : ViewModel() {

    private val _state = MutableStateFlow(ChatUiState())
    val state: StateFlow<ChatUiState> = _state.asStateFlow()

    private var generationJob: Job? = null
    private var generatingIndex = -1

    init {
        refreshNodeStatus()
    }

    /** 重新读取节点端点与已加载模型（从设置/模型管理返回时调用）。 */
    fun refreshNodeStatus() {
        viewModelScope.launch {
            _state.update { it.copy(checkingNode = true, nodeError = null) }
            try {
                val endpoint = endpointProvider()
                val status = client.fetchModelStatus(endpoint)
                _state.update {
                    it.copy(
                        checkingNode = false,
                        endpoint = endpoint.baseUrl,
                        nodeModel = status.model,
                        nodeStatus = status.nodeStatus,
                        nodeError = status.error,
                    )
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Throwable) {
                _state.update {
                    it.copy(
                        checkingNode = false,
                        nodeModel = null,
                        nodeError = "无法连接本机节点服务：${e.message ?: e::class.simpleName}",
                    )
                }
            }
        }
    }

    fun onInputChange(text: String) {
        _state.update { it.copy(inputText = text) }
    }

    fun onSend() {
        val text = _state.value.inputText.trim()
        if (text.isEmpty() || _state.value.isGenerating) return
        val model = _state.value.nodeModel
        if (model == null) {
            appendError("节点没有已加载的 chat 模型，请到「节点设置」选择模型并保存")
            return
        }

        val userMsg = ChatMessage.user(text)
        val assistantMsg = ChatMessage.assistant("")
        val list = _state.value.messages + userMsg + assistantMsg
        generatingIndex = list.lastIndex
        _state.update { it.copy(messages = list, inputText = "", isGenerating = true) }

        // 请求体 = 系统提示 + 历史（含本条用户消息）；最后一条空的 assistant 占位不发送。
        val payload = buildList {
            add(ContractChatMessage(role = "system", content = SYSTEM_PROMPT))
            list.dropLast(1).forEach {
                add(ContractChatMessage(role = it.role.name.lowercase(), content = it.content))
            }
        }

        generationJob = viewModelScope.launch {
            try {
                val endpoint = endpointProvider()
                _state.update { it.copy(endpoint = endpoint.baseUrl) }
                client.streamChat(endpoint, model, payload) { chunk ->
                    _state.update { s ->
                        val msgs = s.messages.toMutableList()
                        if (generatingIndex in msgs.indices) {
                            val cur = msgs[generatingIndex]
                            msgs[generatingIndex] = cur.copy(content = cur.content + chunk)
                        }
                        s.copy(messages = msgs)
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: NodeChatException) {
                appendError(describe(e))
            } catch (e: Throwable) {
                appendError("请求本机节点失败：${e.message ?: e::class.simpleName}")
            } finally {
                _state.update { it.copy(isGenerating = false) }
                generatingIndex = -1
                generationJob = null
            }
        }
    }

    /** 停止生成：直接取消 HTTP 请求（服务端会因客户端断开停止推理），不触碰引擎单例。 */
    fun onStop() {
        generationJob?.cancel()
        generationJob = null
        _state.update { it.copy(isGenerating = false) }
        generatingIndex = -1
    }

    private fun describe(e: NodeChatException): String = when (e.status) {
        401 -> "节点鉴权失败（401）：请到「节点设置」确认 token，并保存后重启服务"
        429 -> "节点并发已满（429）：请稍后重试"
        501 -> "节点不支持该能力（501）：${e.message}"
        503 -> "节点模型不可用（503）：${e.message}"
        else -> "节点返回错误（${e.status}）：${e.message}"
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
        generationJob?.cancel()
        client.close()
    }

    private companion object {
        const val SYSTEM_PROMPT = "你是一个运行在手机上的本地离线 AI 助手，回答简洁准确。"
    }
}
