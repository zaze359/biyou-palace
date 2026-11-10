package com.biyou.sanxian.llm

import android.util.Log
import com.biyou.sanxian.node.EmbeddingEngine
import com.biyou.sanxian.node.EmbeddingResult
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.util.concurrent.atomic.AtomicBoolean

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

class AndroidLlmEngine(private var config: LlmConfig) : LlmEngine, EmbeddingEngine {

    private var handle: Long = 0L
    // Plan A：与 chat 句柄隔离的独立 embedding 上下文。embed() 只走它，避免
    // llama_encode（embedding）与 llama_decode（chat）互相污染 KV。
    @Volatile
    private var embeddingHandle: Long = 0L

    /**
     * 原生调用互斥锁：llama.cpp 的 model/context/sampler 链不是线程安全的，
     * 同一句柄上并发跑两次 generate（或两次 embed）会破坏采样链（实测 scudo 报
     * heap corruption 后 abort）。HTTP 层的并发闸门只负责「允许几路在途 + 429」，
     * 真正进 native 的调用必须在这里串行化。
     *
     * chat 与 embedding 用两把锁：二者是不同的 model/context 句柄，彼此不共享
     * sampler 链，串行化只会白白牺牲吞吐。
     */
    private val chatLock = Mutex()
    private val embedLock = Mutex()

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
            enableEmbeddings = false,
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

        // Plan A：独立 embedding 句柄。embed 与 chat 分属两个上下文，KV 互不污染；
        // 加载失败仅影响 embedding 能力，不拖累 chat（节点仍可正常聊天）。
        if (!config.embeddingModelPath.isNullOrBlank()) {
            val eh = LlamaNative.loadModel(
                path = config.embeddingModelPath,
                nCtx = config.contextSize,
                nThreads = config.threads,
                nBatch = EMBEDDING_N_BATCH,
                nGpuLayers = config.gpuLayers,
                useMmap = config.useMmap,
                kvCacheType = config.kvCacheType.nativeValue,
                temperature = config.temperature,
                topP = config.topP,
                enableEmbeddings = true,
            )
            if (eh == 0L) {
                Log.e(TAG, "embedding 模型加载失败（路径错误/格式不支持/内存不足）：${config.embeddingModelPath}")
            } else {
                embeddingHandle = eh
                Log.i(TAG, "embedding 模型已加载 handle=$embeddingHandle")
            }
        }

        val tmpl = LlamaNative.modelChatTemplate(handle)
        Log.i(
            TAG,
            "模型已加载 handle=$handle desc=${LlamaNative.modelDesc(handle)} " +
                "chatTemplate=${if (tmpl.isBlank()) "无（将回退 ChatML）" else "内嵌 ${tmpl.length} 字符"}",
        )
    }

    override fun isModelLoaded(): Boolean = handle != 0L

    override fun generate(
        session: ChatSession,
        prompt: String,
        maxTokens: Int,
        stats: GenerateStats?,
    ): Flow<String> {
        if (handle == 0L) throw LlmError.NotLoaded()

        val effectiveMax = if (maxTokens > 0) minOf(maxTokens, config.maxTokens) else config.maxTokens

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
        // 本请求是否已真正进入 native：排队等 chatLock 期间不算，否则一个排队请求被取消时
        // 会调用全局 stopGenerate，把正在跑的另一路生成误杀。
        val inNative = AtomicBoolean(false)
        val job = scope.launch {
            try {
                chatLock.withLock {
                    inNative.set(true)
                    try {
                        val rc = LlamaNative.generate(handle, roles, contents, effectiveMax, sink)
                        // 真实用量：prompt 取原生裁剪后的 token 数，completion 取实际生成数。
                        // 在锁内读取，避免下一路请求覆盖 lastPromptTokens。
                        stats?.let {
                            it.promptTokens = LlamaNative.lastPromptTokens(handle)
                            it.completionTokens = if (rc > 0) rc else 0
                        }
                        if (rc == -3) channel.close(LlmError.Cancelled())
                        else if (rc < 0) channel.close(LlmError.GenerateFailed("native code $rc"))
                        else channel.close()
                    } finally {
                        inNative.set(false)
                    }
                }
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
                if (inNative.get()) LlamaNative.stopGenerate(handle)
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
        if (embeddingHandle != 0L) {
            LlamaNative.freeModel(embeddingHandle)
            embeddingHandle = 0L
        }
    }

    /** embedding 句柄是否已加载；health 的 models[].loaded 据此如实上报。 */
    override val isLoaded: Boolean
        get() = embeddingHandle != 0L

    /**
     * 批量向量化（实现 [EmbeddingEngine]，供 [com.biyou.sanxian.node.NodeServer] 的
     * `/v1/embeddings` 路由调用）。
     *
     * 走独立 embedding 句柄（与 chat 隔离）。空串以空格兜底，保证输出维度一致——
     * 原生层对空输入会返回长度 0 向量，会让下游客户端维度错位。
     *
     * @throws LlmError.NotLoaded 当 embedding 模型未加载（配置缺失或加载失败）
     */
    override suspend fun embed(texts: List<String>): EmbeddingResult = embedLock.withLock {
        if (embeddingHandle == 0L) throw LlmError.NotLoaded()
        var promptTokens = 0
        val vectors = texts.map { raw ->
            val text = if (raw.isBlank()) " " else raw
            val v = LlamaNative.embed(embeddingHandle, text).toList()
            // 原生在截断（n_ubatch）后记录真实 token 数；锁内读取，避免下一批覆盖
            promptTokens += LlamaNative.lastPromptTokens(embeddingHandle)
            v
        }
        EmbeddingResult(vectors = vectors, promptTokens = promptTokens)
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
        /**
         * embedding 句柄的 n_ubatch。原生 embed 会按实际分词结果截断到该上限，
         * 避免 llama_encode 触发 GGML_ASSERT(n_ubatch >= n_tokens) 而 abort 进程；
         * 取 1024 是在手机内存与「长文本少截断」之间的折中（chat 句柄仍为 512）。
         */
        const val EMBEDDING_N_BATCH = 1024
    }
}
