package com.biyou.sanxian.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.biyou.sanxian.contract.ChatMessage as ContractChatMessage
import com.biyou.sanxian.contract.ChatContent
import com.biyou.sanxian.llm.ChatMessage
import com.biyou.sanxian.orchestration.ResultEnvelope
import com.biyou.sanxian.orchestration.TaskEvent
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.yield
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive

/** 集群任务在界面上的最小状态；完整事件留在阵图。 */
data class ClusterTaskUiState(
    val taskId: String,
    val status: String = "submitted",
    val startedSteps: Int = 0,
    val completedSteps: Int = 0,
    val failedSteps: Int = 0,
    val runningStep: String? = null,
    val lastEventType: String? = null,
    val error: String? = null,
) {
    val terminal: Boolean get() = status in TERMINAL_STATUSES
    val shortId: String get() = if (taskId.length <= 12) taskId else taskId.take(12) + "…"
}

private val TERMINAL_STATUSES = setOf("completed", "partial", "failed", "canceled")

/**
 * 对话界面 ViewModel。
 *
 * 聊天**只走节点自身的 HTTP 端点**（`http://127.0.0.1:<端口>/v1/chat/completions`，契约原则 2），
 * 不再直连进程内引擎单例——因此聊天页无法、也不允许切换正在对外服务的模型：
 * 模型状态只从 `/v1/health` 只读获取，切换模型需到「节点设置」保存并由前台服务重启加载。
 *
 * 集群模式只负责提交、订阅事件、取消与恢复；规划/选路/聚合全部在阵图，前端不做。
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
    val mode: ChatMode = ChatMode.LOCAL,
    val clusterConfigured: Boolean = false,
    val clusterTask: ClusterTaskUiState? = null,
)

class ChatViewModel(
    private val client: NodeChatClient = KtorNodeChatClient(),
    private val clusterClient: ClusterOrchestrationClient = KtorClusterOrchestrationClient(),
    private val clusterConfigProvider: suspend () -> ClusterSettings = { loadClusterSettings() },
    private val clusterTaskStore: ClusterTaskStore? = defaultClusterTaskStore(),
    private val endpointProvider: suspend () -> NodeChatEndpoint = { loadNodeChatEndpoint() },
) : ViewModel() {

    private val _state = MutableStateFlow(ChatUiState())
    val state: StateFlow<ChatUiState> = _state.asStateFlow()

    private var generationJob: Job? = null
    private var generatingIndex = -1
    private var activeClusterEndpoint: ZhentuEndpoint? = null

    init {
        refreshNodeStatus()
        loadClusterConfigAndResume()
    }

    /** 重新读取节点端点与已加载模型（从设置/模型管理返回时调用）。 */
    fun refreshNodeStatus() {
        viewModelScope.launch {
            // 设置页保存后集群配置可能变化，同步一次「是否已配置」提示。
            runCatching { clusterConfigProvider() }.getOrNull()?.let { cluster ->
                _state.update { it.copy(clusterConfigured = cluster.configured) }
            }
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

    fun onModeChange(mode: ChatMode) {
        _state.update { it.copy(mode = mode) }
    }

    fun onSend() {
        val text = _state.value.inputText.trim()
        if (text.isEmpty() || _state.value.isGenerating) return
        when (_state.value.mode) {
            ChatMode.LOCAL -> sendLocal(text)
            ChatMode.CLUSTER -> sendCluster(text, requestedMode = ChatMode.CLUSTER, allowFallback = false)
            ChatMode.AUTO -> sendCluster(text, requestedMode = ChatMode.AUTO, allowFallback = true)
        }
    }

    private fun sendLocal(text: String) {
        val model = _state.value.nodeModel
        if (model == null) {
            appendError("节点没有已加载的 chat 模型，请到「节点设置」选择模型并保存")
            return
        }
        val index = appendUserAndPlaceholder(text)
        val history = _state.value.messages.dropLast(1)
        launchLocal(index, model, history)
    }

    private fun sendCluster(text: String, requestedMode: ChatMode, allowFallback: Boolean) {
        val index = appendUserAndPlaceholder(text)
        val history = _state.value.messages.dropLast(1)
        generationJob = viewModelScope.launch {
            try {
                submitAndRun(index, history, requestedMode, allowFallback)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Throwable) {
                updateMessage(index, "[错误] ${describeCluster(e.toClusterException())}")
            } finally {
                finishGeneration()
            }
        }
    }

    private fun appendUserAndPlaceholder(text: String): Int {
        val list = _state.value.messages + ChatMessage.user(text) + ChatMessage.assistant("")
        generatingIndex = list.lastIndex
        _state.update { it.copy(messages = list, inputText = "", isGenerating = true) }
        return list.lastIndex
    }

    private fun launchLocal(index: Int, model: String, history: List<ChatMessage>) {
        generationJob = viewModelScope.launch {
            try {
                streamLocalInto(index, model, history)
            } catch (e: CancellationException) {
                throw e
            } catch (e: NodeChatException) {
                updateMessage(index, "[错误] ${describeLocal(e)}")
            } catch (e: Throwable) {
                updateMessage(index, "[错误] 请求本机节点失败：${e.message ?: e::class.simpleName}")
            } finally {
                finishGeneration()
            }
        }
    }

    private suspend fun streamLocalInto(index: Int, model: String, history: List<ChatMessage>) {
        val payload = buildList {
            add(ContractChatMessage(role = "system", content = ChatContent.Text(SYSTEM_PROMPT)))
            history.forEach {
                add(ContractChatMessage(role = it.role.name.lowercase(), content = ChatContent.Text(it.content)))
            }
        }
        val endpoint = endpointProvider()
        _state.update { it.copy(endpoint = endpoint.baseUrl) }
        client.streamChat(endpoint, model, payload) { chunk ->
            updateMessage(index, _state.value.messages.getOrNull(index)?.content.orEmpty() + chunk)
        }
    }

    // ------------------------------------------------------------ 集群流程

    private suspend fun submitAndRun(
        index: Int,
        history: List<ChatMessage>,
        requestedMode: ChatMode,
        allowFallback: Boolean,
    ) {
        val config = runCatching { clusterConfigProvider() }.getOrElse {
            if (allowFallback) {
                fallbackToLocal(index, history, "读取阵图配置失败")
            } else {
                updateMessage(index, "[错误] 读取阵图配置失败：${it.message ?: it::class.simpleName}")
            }
            return
        }
        if (!config.configured) {
            if (allowFallback) {
                fallbackToLocal(index, history, "未配置阵图地址")
            } else {
                updateMessage(index, "[错误] 未配置阵图地址，请到「节点设置」填写阵图地址与 token")
            }
            return
        }
        _state.update { it.copy(clusterConfigured = true) }

        val endpoint = ZhentuEndpoint(config.baseUrl, config.token, config.originNodeId)
        val envelope = runCatching {
            ClusterRequestEnvelopeBuilder.build(
                originNodeId = config.originNodeId,
                requestedMode = requestedMode,
                messages = history,
            )
        }.getOrElse {
            updateMessage(index, "[错误] 无法构造集群请求：${it.message}")
            return
        }

        updateMessage(index, "正在提交阵图…")
        val submit = try {
            clusterClient.submit(endpoint, envelope)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Throwable) {
            val error = e.toClusterException()
            if (allowFallback && error.beforeTaskCreated) {
                fallbackToLocal(index, history, describeCluster(error))
            } else {
                updateMessage(index, "[错误] ${describeCluster(error)}")
            }
            return
        }

        activeClusterEndpoint = endpoint
        runClusterTask(index, endpoint, submit, lastEventId = null)
    }

    private suspend fun runClusterTask(
        index: Int,
        endpoint: ZhentuEndpoint,
        submit: ClusterSubmit,
        lastEventId: String?,
    ) {
        _state.update {
            it.copy(
                clusterTask = ClusterTaskUiState(
                    taskId = submit.taskId,
                    status = "submitted",
                ),
            )
        }
        updateMessage(index, "已提交阵图任务 ${shortId(submit.taskId)}，等待事件…")
        persist(submit.taskId, lastEventId, "submitted")

        var cursor = lastEventId
        var streamError: ClusterException? = null
        try {
            clusterClient.streamEvents(endpoint, submit.eventsUrl, lastEventId) { event ->
                applyEvent(event)
                cursor = event.eventId
                persist(submit.taskId, cursor, event.type.toStatus())
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Throwable) {
            val error = e.toClusterException()
            // 410 表示事件窗口已过期，任务可能已结束：直接读最终结果。
            if (error.status != 410) streamError = error
        }

        streamError?.let { error ->
            _state.update { s ->
                s.copy(
                    clusterTask = s.clusterTask?.copy(
                        status = "interrupted",
                        error = describeCluster(error),
                    ),
                )
            }
            updateMessage(
                index,
                "集群任务 ${shortId(submit.taskId)} 的事件流中断：${describeCluster(error)}。\n" +
                    "任务已保留，可稍后恢复（task_id ${submit.taskId}）。",
            )
            persist(submit.taskId, cursor, "interrupted")
            return
        }

        val result = fetchResult(endpoint, submit.resultUrl)
        if (result == null) {
            updateMessage(index, "集群任务已结束，但读取最终结果失败。task_id ${submit.taskId}")
            persist(submit.taskId, cursor, "unknown")
            return
        }
        renderClusterResult(index, result)
        persist(submit.taskId, cursor, result.status, result.answer)
        _state.update { s ->
            s.copy(
                clusterTask = s.clusterTask?.copy(
                    status = result.status,
                    error = result.error?.message,
                ),
            )
        }
    }

    private suspend fun fetchResult(endpoint: ZhentuEndpoint, resultUrl: String): ResultEnvelope? {
        repeat(3) {
            val result = runCatching { clusterClient.getResult(endpoint, resultUrl) }.getOrNull()
            if (result != null) return result
            yield()
        }
        return null
    }

    private fun applyEvent(event: TaskEvent) {
        _state.update { s ->
            val current = s.clusterTask ?: return@update s
            val next = when (event.type) {
                "step/started" -> current.copy(
                    status = "running",
                    startedSteps = current.startedSteps + 1,
                    runningStep = event.stepId,
                )
                "step/result" -> current.copy(
                    completedSteps = current.completedSteps + 1,
                    runningStep = null,
                )
                "step/failed" -> current.copy(
                    failedSteps = current.failedSteps + 1,
                    runningStep = null,
                )
                "aggregate/started" -> current.copy(status = "aggregating")
                "task/completed" -> current.copy(status = "completed", runningStep = null)
                "task/failed" -> current.copy(status = "failed", runningStep = null)
                "task/canceled" -> current.copy(status = "canceled", runningStep = null)
                else -> current
            }.copy(lastEventType = event.type)
            s.copy(clusterTask = next)
        }
    }

    private fun renderClusterResult(index: Int, result: ResultEnvelope) {
        val answer = result.answer?.takeIf { it.isNotBlank() }
        val body = when {
            answer != null -> answer
            result.status == "canceled" -> "集群任务已取消。"
            result.status == "failed" -> "集群任务失败：${result.error?.message ?: result.terminalReason ?: "未知原因"}"
            result.status == "partial" -> "集群任务部分完成，未生成最终答案。"
            else -> "集群任务已结束（${result.status}）。"
        }
        val extra = buildString {
            if (result.sources.isNotEmpty()) {
                append("\n\n来源：")
                append(result.sources.joinToString("；") { "${it.stepId} @ ${it.nodeId}/${it.model}" })
            }
            if (result.partials.isNotEmpty()) {
                append("\n未完成：")
                append(result.partials.joinToString("；") { "${it.stepId}（${it.reason}）" })
            }
        }
        updateMessage(index, body + extra)
    }

    private suspend fun fallbackToLocal(index: Int, history: List<ChatMessage>, reason: String) {
        val model = _state.value.nodeModel
        if (model == null) {
            updateMessage(
                index,
                "[错误] 集群提交失败（$reason），且本机没有已加载模型，无法回退本地。",
            )
            return
        }
        updateMessage(index, "")
        try {
            streamLocalInto(index, model, history)
            val content = _state.value.messages.getOrNull(index)?.content.orEmpty()
            updateMessage(index, "[已回退本地] $reason\n\n$content")
        } catch (e: CancellationException) {
            throw e
        } catch (e: Throwable) {
            updateMessage(index, "[错误] 回退本地失败：${e.message ?: e::class.simpleName}")
        }
    }

    // ------------------------------------------------------------ 取消与恢复
    /** 停止生成：本地取消 HTTP 请求；集群任务调用阵图 `/cancel`，等待终态事件。 */
    fun onStop() {
        val task = _state.value.clusterTask
        val endpoint = activeClusterEndpoint
        if (task != null && !task.terminal && endpoint != null) {
            _state.update { it.copy(clusterTask = it.clusterTask?.copy(status = "canceling")) }
            viewModelScope.launch {
                runCatching { clusterClient.cancel(endpoint, task.taskId) }
                    .onFailure { updateClusterError(describeCluster(it.toClusterException())) }
            }
            return
        }
        generationJob?.cancel()
        generationJob = null
        _state.update { it.copy(isGenerating = false) }
        generatingIndex = -1
    }

    private fun loadClusterConfigAndResume() {
        viewModelScope.launch {
            val config = runCatching { clusterConfigProvider() }.getOrNull() ?: return@launch
            _state.update {
                it.copy(
                    clusterConfigured = config.configured,
                    mode = if (config.configured) config.defaultMode else ChatMode.LOCAL,
                )
            }
            if (!config.configured) return@launch

            val saved = runCatching { clusterTaskStore?.load() }.getOrNull() ?: return@launch
            if (saved.status in TERMINAL_STATUSES) {
                runCatching { clusterTaskStore?.clear() }
                return@launch
            }

            val endpoint = ZhentuEndpoint(config.baseUrl, config.token, config.originNodeId)
            activeClusterEndpoint = endpoint
            val index = _state.value.messages.size
            generatingIndex = index
            _state.update {
                it.copy(
                    messages = it.messages + ChatMessage.assistant("正在恢复集群任务 ${shortId(saved.taskId)}…"),
                    isGenerating = true,
                    clusterTask = ClusterTaskUiState(
                        taskId = saved.taskId,
                        status = saved.status.ifBlank { "running" },
                    ),
                )
            }
            val submit = ClusterSubmit(
                taskId = saved.taskId,
                eventsUrl = "/zhentu/v1/requests/${saved.taskId}/events",
                resultUrl = "/zhentu/v1/requests/${saved.taskId}",
                duplicate = true,
            )
            generationJob = viewModelScope.launch {
                try {
                    runClusterTask(index, endpoint, submit, saved.lastEventId)
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Throwable) {
                    updateClusterError(describeCluster(e.toClusterException()))
                } finally {
                    finishGeneration()
                }
            }
        }
    }

    private suspend fun persist(
        taskId: String,
        lastEventId: String?,
        status: String,
        answer: String? = null,
    ) {
        clusterTaskStore?.save(PersistedClusterTask(taskId, lastEventId, status, answer))
    }

    // ------------------------------------------------------------ 工具

    private fun updateMessage(index: Int, text: String) {
        _state.update { s ->
            if (index !in s.messages.indices) return@update s
            val messages = s.messages.toMutableList()
            messages[index] = messages[index].copy(content = text)
            s.copy(messages = messages)
        }
    }

    private fun updateClusterError(message: String) {
        _state.update { s ->
            s.copy(clusterTask = s.clusterTask?.copy(error = message))
        }
    }

    private fun finishGeneration() {
        _state.update { it.copy(isGenerating = false) }
        generatingIndex = -1
        generationJob = null
        activeClusterEndpoint = null
    }

    private fun describeLocal(e: NodeChatException): String = when (e.status) {
        401 -> "节点鉴权失败（401）：请到「节点设置」确认 token，并保存后重启服务"
        429 -> "节点并发已满（429）：请稍后重试"
        501 -> "节点不支持该能力（501）：${e.message}"
        503 -> "节点模型不可用（503）：${e.message}"
        else -> "节点返回错误（${e.status}）：${e.message}"
    }

    private fun describeCluster(e: ClusterException): String = when (e.status) {
        401 -> "阵图鉴权失败（401）：请到「节点设置」确认阵图 token"
        403 -> "阵图拒绝访问（403）：请确认本节点 id 与阵图 node_tokens 一致"
        404 -> "阵图任务不存在（404）：可能已被清理"
        410 -> "事件重放已过期（410）：将直接读取最终结果"
        429 -> "阵图任务容量已满（429）：请稍后重试"
        0 -> "无法连接阵图：${e.message}"
        else -> "阵图返回错误（${e.status}）：${e.message}"
    }

    private fun Throwable.toClusterException(): ClusterException =
        this as? ClusterException
            ?: ClusterException(0, null, message ?: this::class.simpleName ?: "未知错误")

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

    private fun shortId(taskId: String): String =
        if (taskId.length <= 12) taskId else taskId.take(12) + "…"

    override fun onCleared() {
        super.onCleared()
        generationJob?.cancel()
        client.close()
        clusterClient.close()
    }

    private companion object {
        const val SYSTEM_PROMPT = "你是一个运行在手机上的本地离线 AI 助手，回答简洁准确。"
    }
}

private val TaskEvent.stepId: String?
    get() = runCatching { data["step_id"]?.jsonPrimitive?.contentOrNull }.getOrNull()

private fun String.toStatus(): String = when (this) {
    "task/completed" -> "completed"
    "task/failed" -> "failed"
    "task/canceled" -> "canceled"
    else -> "running"
}
