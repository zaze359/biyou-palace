package com.biyou.sanxian.ui

import com.biyou.sanxian.contract.ChatMessage
import com.biyou.sanxian.llm.LlmEngineProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
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
        assertEquals("你好", client.lastMessages.last().content)
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
}
