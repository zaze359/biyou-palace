package com.smallphone.llm

import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.launch

/**
 * Android 平台 [LlmEngine] 实现：通过 JNI 调用 libllama_jni.so（内含 llama.cpp + ggml）。
 *
 * 推理链路：
 *  - loadModel() 加载 GGUF 模型，原生侧建立 model/context/sampler 并持有句柄；
 *  - generate() 把「会话消息数组」整体交给原生层，由原生在一次调用内完成「输入 token 预算 →
 *    按消息粒度裁剪（保全 system 与最新提问）→ chat template 渲染 → 分词 → eval → 逐 token
 *    采样」，每产出一个 token 经 [TokenSink] 送回 Kotlin，由 Channel
 *    泵到 Flow 供 Compose UI 逐字渲染；
 *  - stopGeneration() 通过原生 [LlamaNative.stopGenerate] 设置取消标志，原生采样循环
 *    检测到后退出并回传 -3。
 *
 * 为什么不在 Kotlin 侧渲染 prompt：上下文裁剪必须发生在渲染**之前**（按消息丢，而不是
 * 按 token 裸切），而「裁到哪」又要看分词结果 —— 三者只有原生侧能闭环。详见
 * `.trellis/tasks/09-28-fix-context-overflow-window/design.md`。
 */
actual fun createLlmEngine(config: LlmConfig): LlmEngine = AndroidLlmEngine(config)

class AndroidLlmEngine(private var config: LlmConfig) : LlmEngine {

    private var handle: Long = 0L

    override fun loadModel(config: LlmConfig) {
        if (config.modelPath.isBlank()) {
            throw LlmError.ModelNotFound(config.modelPath)
        }
        // 单例引擎可能被重复 loadModel（切换模型）：先释放旧模型，避免原生句柄泄漏。
        close()
        val h = LlamaNative.loadModel(
            path = config.modelPath,
            nCtx = config.contextSize,
            nThreads = config.threads,
            nBatch = 512,
            nGpuLayers = config.gpuLayers,
            useMmap = config.useMmap,
            kvCacheType = config.kvCacheType.nativeValue,
            temperature = config.temperature,
            topP = config.topP,
        )
        // D4：以完整 LlmConfig 应用采样参数（top-k / 重复惩罚 / 可控 seed），重建原生采样链。
        // loadModel 内部已用 D4 默认值兜底，此处用真实 config 覆盖。
        LlamaNative.setSamplingParams(
            handle = h,
            temperature = config.temperature,
            topP = config.topP,
            topK = config.topK,
            repeatPenalty = config.repeatPenalty,
            repeatLastN = config.repeatLastN,
            seed = config.seed,
        )
        if (h == 0L) {
            throw LlmError.LoadFailed("native loadModel 返回 0（路径错误 / 格式不支持 / 内存不足）")
        }
        handle = h
        this.config = config   // 回写本次加载配置，使 getModelInfo()/generate() 与之一致
        val tmpl = LlamaNative.modelChatTemplate(handle)
        Log.i(
            TAG,
            "模型已加载 handle=$handle desc=${LlamaNative.modelDesc(handle)} " +
                "chatTemplate=${if (tmpl.isBlank()) "无（将回退 ChatML）" else "内嵌 ${tmpl.length} 字符"}",
        )
    }

    override fun isModelLoaded(): Boolean = handle != 0L

    override fun generate(session: ChatSession, prompt: String): Flow<String> {
        if (handle == 0L) throw LlmError.NotLoaded()

        val messages = buildMessageList(session, prompt)
        if (messages.isEmpty()) throw LlmError.GenerateFailed("会话消息为空")

        val roles = Array(messages.size) { messages[it].role.name.lowercase() }
        val contents = Array(messages.size) { messages[it].content }

        // 原生 generate 是同步阻塞调用：在独立 IO 协程上运行，每产出一个 token 经
        // TokenSink 投递到 Channel；flow 块只负责从 Channel 取出并 emit，供 Compose UI 逐字渲染。
        val channel = Channel<String>(Channel.UNLIMITED)
        val sink = TokenSink { piece ->
            channel.trySend(piece)
            true
        }
        val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
        val job = scope.launch {
            try {
                val rc = LlamaNative.generate(handle, roles, contents, config.maxTokens, sink)
                if (rc == -3) channel.close(LlmError.Cancelled())
                else if (rc < 0) channel.close(LlmError.GenerateFailed("native code $rc"))
                else channel.close()
            } catch (e: Throwable) {
                Log.e(TAG, "native generate 异常", e)
                channel.close(e)
            }
        }
        // 收集方取消（退出界面 / ViewModel 清理）时，停止原生生成并取消后台协程
        return flow {
            try {
                for (piece in channel) {
                    emit(piece)
                }
            } finally {
                LlamaNative.stopGenerate(handle)
                job.cancel()
            }
        }
    }

    override fun stopGeneration() {
        if (handle != 0L) LlamaNative.stopGenerate(handle)
    }

    override fun getModelInfo(): ModelInfo? {
        if (handle == 0L) return null
        val gpu = if (config.gpuLayers > 0) " (GPU x${config.gpuLayers})" else " (CPU)"
        val kv = " | KV=${config.kvCacheType.name}"
        return ModelInfo(
            name = LlamaNative.modelDesc(handle),
            contextSize = config.contextSize,
            backend = "llama.cpp$gpu$kv",
        )
    }

    override fun close() {
        if (handle != 0L) {
            LlamaNative.freeModel(handle)
            handle = 0L
        }
    }

    /**
     * 组装本轮要交给原生层的消息数组。
     *
     * [ChatSession.messages] 已包含最新一条用户消息，因此不重复拼接 [prompt]；
     * 仅当消息为空时才用 [prompt] 兜底，避免把空数组传到原生层。
     *
     * **这里只组装、不渲染**：chat template 渲染与上下文裁剪都由原生层完成——只有原生
     * 才知道 `n_ctx`、`maxTokens`，也只有它持有分词器。
     */
    private fun buildMessageList(session: ChatSession, prompt: String): List<ChatMessage> = buildList {
        if (!session.systemPrompt.isNullOrBlank()) {
            add(ChatMessage(role = Role.System, content = session.systemPrompt))
        }
        addAll(session.messages)
        // 兜底用 [prompt] 仅在「列表里还没有任何用户提问」时补齐，避免出现 system 在、
        // 用户问题却被静默丢掉、最终只剩 [system] 的退化情况（那样 system 会被当成
        // 最新消息并可能在裁剪时被切）
        if (none { it.role == Role.User }) {
            add(ChatMessage(role = Role.User, content = prompt))
        }
    }

    private companion object {
        const val TAG = "AndroidLlmEngine"
    }
}
