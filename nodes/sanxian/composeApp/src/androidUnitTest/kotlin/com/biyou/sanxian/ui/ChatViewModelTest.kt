package com.biyou.sanxian.ui

import com.biyou.sanxian.contract.ChatMessage
import com.biyou.sanxian.llm.LlmEngineProvider
import com.biyou.sanxian.orchestration.RequestEnvelope
import com.biyou.sanxian.orchestration.ResultEnvelope
import com.biyou.sanxian.orchestration.ResultSource
import com.biyou.sanxian.orchestration.ResultUsage
import com.biyou.sanxian.orchestration.TaskEvent
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * 聊天 ViewModel 回归：
 *  1) 聊天只走节点 HTTP 端点，绝不触碰进程内引擎单例；
 *  2) 聊天页不再提供「加载/切换模型」入口（换模型只能走设置页的受控重启路径）。
 */
@OptIn(ExperimentalCoroutinesApi::class)
class ChatViewModelTest {

    private class FakeChatClient(
        private val status: NodeModelStatus,
        private val deltas: List<String> = listOf("你", "好"),
    ) : NodeChatClient {
        var lastEndpoint: NodeChatEndpoint? = null
        var lastModel: String? = null
        var lastMessages: List<ChatMessage> = emptyList()
        var closed = false

        override suspend fun fetchModelStatus(endpoint: NodeChatEndpoint): NodeModelStatus = status

        override suspend fun streamChat(
            endpoint: NodeChatEndpoint,
            model: String,
            messages: List<ChatMessage>,
            onDelta: suspend (String) -> Unit,
        ) {
            lastEndpoint = endpoint
            lastModel = model
            lastMessages = messages
            deltas.forEach { onDelta(it) }
        }

        override fun close() {
            closed = true
        }
    }

    @BeforeTest
    fun setUp() {
        Dispatchers.setMain(UnconfinedTestDispatcher())
    }

    @AfterTest
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun `聊天走本机 HTTP 端点且不初始化引擎单例`() {
        assertFalse(LlmEngineProvider.isInitialized, "测试前置：引擎单例不应已被创建")

        val client = FakeChatClient(NodeModelStatus("Qwen3-0.6B-Q8_0.gguf", "ready"))
        val endpoint = NodeChatEndpoint("http://127.0.0.1:4031/v1", "token-from-config")
        val vm = ChatViewModel(client) { endpoint }

        // init 只读 /v1/health 拿模型状态
        assertEquals("Qwen3-0.6B-Q8_0.gguf", vm.state.value.nodeModel)
        assertEquals("http://127.0.0.1:4031/v1", vm.state.value.endpoint)

        vm.onInputChange("你好")
        vm.onSend()

        assertEquals(endpoint, client.lastEndpoint)
        assertEquals("Qwen3-0.6B-Q8_0.gguf", client.lastModel)
        assertEquals(listOf("system", "user"), client.lastMessages.map { it.role })
        assertEquals("你好", client.lastMessages.last().content.plainText)
        assertEquals("你好", vm.state.value.messages.last().content)
        assertFalse(vm.state.value.isGenerating)
        assertFalse(LlmEngineProvider.isInitialized, "聊天链路不得触碰引擎单例")
    }

    @Test
    fun `节点无已加载模型时不发送请求并提示去设置`() {
        val client = FakeChatClient(NodeModelStatus(null, "loading", "节点当前没有已加载的 chat 模型"))
        val vm = ChatViewModel(client) { NodeChatEndpoint("http://127.0.0.1:4031/v1", null) }

        vm.onInputChange("你好")
        vm.onSend()

        assertNull(client.lastModel, "无模型时不得发起 chat 请求")
        assertTrue(vm.state.value.messages.last().content.contains("节点设置"))
        assertFalse(LlmEngineProvider.isInitialized)
    }

    @Test
    fun `聊天页不再暴露加载或切换模型入口`() {
        val methods = ChatViewModel::class.java.declaredMethods.map { it.name }
        assertFalse(methods.contains("loadModel"), "聊天页不应再能加载模型")
        assertFalse(methods.contains("onModelPathChange"), "聊天页不应再能改模型路径")

        val engineFields = ChatViewModel::class.java.declaredFields
            .filter { it.type.name.contains("LlmEngine") }
        assertTrue(engineFields.isEmpty(), "聊天 ViewModel 不应持有引擎引用：$engineFields")
    }

    // ------------------------------------------------------------ 集群模式

    private class FakeClusterClient(
        private val submitFailure: Throwable? = null,
        private val events: List<TaskEvent> = emptyList(),
        private val result: ResultEnvelope = completedResult(),
        private val streamGate: CompletableDeferred<Unit>? = null,
    ) : ClusterOrchestrationClient {
        var submitted: RequestEnvelope? = null
        var canceled: String? = null

        override suspend fun submit(endpoint: ZhentuEndpoint, envelope: RequestEnvelope): ClusterSubmit {
            submitted = envelope
            submitFailure?.let { throw it }
            return ClusterSubmit(
                taskId = "task-1",
                eventsUrl = "/zhentu/v1/requests/task-1/events",
                resultUrl = "/zhentu/v1/requests/task-1",
                duplicate = false,
            )
        }

        override suspend fun streamEvents(
            endpoint: ZhentuEndpoint,
            eventsUrl: String,
            lastEventId: String?,
            onEvent: suspend (TaskEvent) -> Unit,
        ) {
            events.forEach { onEvent(it) }
            streamGate?.await()
        }

        override suspend fun getResult(endpoint: ZhentuEndpoint, resultUrl: String): ResultEnvelope = result

        override suspend fun cancel(endpoint: ZhentuEndpoint, taskId: String) {
            canceled = taskId
        }

        override fun close() = Unit
    }

    private class InMemoryTaskStore(
        private var saved: PersistedClusterTask? = null,
    ) : ClusterTaskStore {
        override suspend fun load(): PersistedClusterTask? = saved
        override suspend fun save(task: PersistedClusterTask) {
            saved = task
        }

        override suspend fun clear() {
            saved = null
        }
    }

    private fun clusterVm(
        cluster: FakeClusterClient,
        config: ClusterSettings = ClusterSettings(
            baseUrl = "http://127.0.0.1:8100",
            token = "zt-token",
            originNodeId = "sanxian-01",
            defaultMode = ChatMode.CLUSTER,
        ),
        chat: FakeChatClient = FakeChatClient(NodeModelStatus(null, "ready")),
        store: ClusterTaskStore = InMemoryTaskStore(),
    ): ChatViewModel = ChatViewModel(
        client = chat,
        clusterClient = cluster,
        clusterConfigProvider = { config },
        clusterTaskStore = store,
        endpointProvider = { NodeChatEndpoint("http://127.0.0.1:4031/v1", null) },
    )

    @Test
    fun `cluster 模式提交并渲染答案与来源`() {
        val cluster = FakeClusterClient(
            events = listOf(
                event(1, "step/started", "s1"),
                event(2, "step/result", "s1"),
                event(3, "aggregate/started"),
                event(4, "task/completed"),
            ),
        )
        val store = InMemoryTaskStore()
        val vm = clusterVm(cluster = cluster, store = store)

        vm.onInputChange("你好")
        vm.onSend()

        assertEquals("cluster", cluster.submitted?.mode)
        assertEquals("cluster", cluster.submitted?.requestedMode)
        val content = vm.state.value.messages.last().content
        assertTrue(content.contains("集群答案"), content)
        assertTrue(content.contains("来源：s1 @ quasheng-01/qwen"), content)
        assertEquals("completed", vm.state.value.clusterTask?.status)
        assertFalse(vm.state.value.isGenerating)
        assertEquals("completed", runBlocking { store.load()?.status })
    }

    @Test
    fun `cluster 提交失败明确报错且不回退本地`() {
        val chat = FakeChatClient(NodeModelStatus("local-model.gguf", "ready"))
        val cluster = FakeClusterClient(submitFailure = ClusterException(500, "boom", "阵图炸了"))
        val vm = clusterVm(cluster = cluster, chat = chat)

        vm.onInputChange("你好")
        vm.onSend()

        assertTrue(vm.state.value.messages.last().content.contains("阵图返回错误（500）"))
        assertNull(chat.lastModel, "cluster 模式失败不得静默回退本地")
        assertFalse(vm.state.value.isGenerating)
    }

    @Test
    fun `auto 无本地模型时提交集群`() {
        val cluster = FakeClusterClient(events = listOf(event(1, "task/completed")))
        val vm = clusterVm(
            cluster = cluster,
            config = ClusterSettings(
                baseUrl = "http://127.0.0.1:8100",
                token = "zt-token",
                originNodeId = "sanxian-01",
                defaultMode = ChatMode.AUTO,
            ),
            chat = FakeChatClient(NodeModelStatus(null, "loading")),
        )

        vm.onInputChange("你好")
        vm.onSend()

        assertEquals(ChatMode.AUTO, vm.state.value.mode)
        assertEquals("auto", cluster.submitted?.requestedMode)
        assertTrue(vm.state.value.messages.last().content.contains("集群答案"))
    }

    @Test
    fun `auto 提交前失败回退本地并标注原因`() {
        val chat = FakeChatClient(NodeModelStatus("local-model.gguf", "ready"))
        val cluster = FakeClusterClient(submitFailure = ClusterException(401, "unauthorized", "token 无效"))
        val vm = clusterVm(
            cluster = cluster,
            config = ClusterSettings(
                baseUrl = "http://127.0.0.1:8100",
                token = "bad",
                originNodeId = "sanxian-01",
                defaultMode = ChatMode.AUTO,
            ),
            chat = chat,
        )

        vm.onInputChange("你好")
        vm.onSend()

        val content = vm.state.value.messages.last().content
        assertTrue(content.startsWith("[已回退本地]"), content)
        assertTrue(content.contains("阵图鉴权失败（401）"), content)
        assertTrue(content.contains("你好"), content)
        assertEquals("local-model.gguf", chat.lastModel)
    }

    @Test
    fun `取消进行中的集群任务会调用阵图 cancel`() {
        val gate = CompletableDeferred<Unit>()
        val cluster = FakeClusterClient(
            events = listOf(event(1, "step/started", "s1")),
            streamGate = gate,
        )
        val vm = clusterVm(cluster = cluster)

        vm.onInputChange("你好")
        vm.onSend()
        assertEquals("running", vm.state.value.clusterTask?.status)

        vm.onStop()
        assertEquals("task-1", cluster.canceled)
        assertEquals("canceling", vm.state.value.clusterTask?.status)

        gate.complete(Unit)
    }

    @Test
    fun `启动时恢复未完成的集群任务且不重复提交`() {
        val store = InMemoryTaskStore(
            PersistedClusterTask("task-1", "evt-1", "running"),
        )
        val cluster = FakeClusterClient(events = listOf(event(2, "task/completed")))
        val vm = clusterVm(cluster = cluster, store = store)

        assertNull(cluster.submitted, "恢复不得重新提交任务")
        assertTrue(vm.state.value.messages.last().content.contains("集群答案"))
        assertEquals("completed", runBlocking { store.load()?.status })
    }

    private companion object {
        fun event(seq: Int, type: String, stepId: String? = null): TaskEvent = TaskEvent(
            eventId = "evt-$seq",
            taskId = "task-1",
            seq = seq,
            type = type,
            at = "2026-11-21T00:00:0${seq}+08:00",
            data = buildJsonObject {
                if (stepId != null) put("step_id", JsonPrimitive(stepId))
            },
        )

        fun completedResult(): ResultEnvelope = ResultEnvelope(
            schemaVersion = "1.0",
            taskId = "task-1",
            status = "completed",
            terminalReason = null,
            mode = "cluster",
            answer = "集群答案",
            answerRef = null,
            sources = listOf(
                ResultSource(
                    stepId = "s1",
                    nodeId = "quasheng-01",
                    model = "qwen",
                ),
            ),
            partials = emptyList(),
            usage = ResultUsage(promptTokens = 1, completionTokens = 2, steps = 1),
            error = null,
        )
    }
}
