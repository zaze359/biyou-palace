package com.biyou.sanxian.llm

import android.util.Log
import com.biyou.sanxian.node.VisionEngine
import com.biyou.sanxian.node.VisionRequestException
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
 * Android 视觉引擎：独立句柄加载「视觉 LLM + mmproj」，经 JNI 调 llama.cpp `tools/mtmd`。
 *
 * 与 [AndroidLlmEngine] 完全隔离：视觉请求不会污染 chat 的 KV/采样链，反之亦然。
 * 原生调用用 [lock] 串行化（mtmd + llama context 非线程安全）；HTTP 侧并发闸门负责排队/429。
 */
class AndroidVisionEngine(private var config: VisionConfig) : VisionEngine {

    private var handle: Long = 0L
    private val lock = Mutex()

    override val mediaMarker: String =
        runCatching { LlamaNative.visionMediaMarker() }.getOrDefault("<__media__>")

    override val isLoaded: Boolean
        get() = handle != 0L && LlamaNative.isVisionReady(handle)

    fun loadModel(config: VisionConfig) {
        if (config.modelPath.isBlank() || config.mmprojPath.isBlank()) {
            throw LlmError.ModelNotFound("${config.modelPath} / ${config.mmprojPath}")
        }
        close()

        val h = LlamaNative.loadModel(
            path = config.modelPath,
            nCtx = config.contextSize,
            nThreads = config.threads,
            nBatch = N_BATCH,
            nGpuLayers = 0,
            useMmap = config.useMmap,
            kvCacheType = config.kvCacheType.nativeValue,
            temperature = config.temperature,
            topP = config.topP,
            enableEmbeddings = false,
        )
        if (h == 0L) {
            throw LlmError.LoadFailed("视觉模型加载失败：${config.modelPath}")
        }
        LlamaNative.setSamplingParams(
            handle = h,
            temperature = config.temperature,
            topP = config.topP,
            topK = config.topK,
            repeatPenalty = config.repeatPenalty,
            repeatLastN = config.repeatLastN,
            seed = config.seed,
        )
        if (!LlamaNative.initVision(h, config.mmprojPath, config.threads, config.imageMaxTokens)) {
            LlamaNative.freeModel(h)
            throw LlmError.LoadFailed("mmproj 加载失败：${config.mmprojPath}")
        }

        handle = h
        this.config = config
        Log.i(TAG, "视觉模型已加载: ${config.modelPath} + ${config.mmprojPath}")
    }

    override fun generate(
        session: ChatSession,
        prompt: String,
        images: List<ByteArray>,
        maxTokens: Int,
        stats: GenerateStats?,
    ): Flow<String> {
        if (handle == 0L) throw LlmError.NotLoaded()

        val messages = buildMessageList(session, prompt)
        if (messages.isEmpty()) throw LlmError.GenerateFailed("会话消息为空")

        val roles = Array(messages.size) { messages[it].role.name.lowercase() }
        val contents = Array(messages.size) { messages[it].content }
        val effectiveMax = if (maxTokens > 0) minOf(maxTokens, config.maxTokens) else config.maxTokens

        val channel = Channel<String>(Channel.UNLIMITED)
        val sink = TokenSink { piece ->
            channel.trySend(piece)
            true
        }
        val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
        val inNative = AtomicBoolean(false)
        val job = scope.launch {
            try {
                lock.withLock {
                    inNative.set(true)
                    try {
                        val rc = LlamaNative.generateVision(
                            handle, roles, contents, images.toTypedArray(), effectiveMax, sink,
                        )
                        stats?.let {
                            it.promptTokens = LlamaNative.lastPromptTokens(handle)
                            it.completionTokens = if (rc > 0) rc else 0
                        }
                        when {
                            rc == -3 -> channel.close(LlmError.Cancelled())
                            rc == -7 -> channel.close(
                                VisionRequestException("图像与文本超出上下文预算（n_ctx=${config.contextSize}）")
                            )
                            rc == -8 -> channel.close(VisionRequestException("图像与文本分词失败"))
                            rc == -9 -> channel.close(VisionRequestException("图像解码失败：格式不支持或文件损坏"))
                            rc < 0 -> channel.close(LlmError.GenerateFailed("native code $rc"))
                            else -> channel.close()
                        }
                    } finally {
                        inNative.set(false)
                    }
                }
            } catch (e: Throwable) {
                Log.e(TAG, "native generateVision 异常", e)
                channel.close(e)
            }
        }

        return flow {
            try {
                for (piece in channel) emit(piece)
            } finally {
                if (inNative.get()) LlamaNative.stopGenerate(handle)
                job.cancel()
            }
        }
    }

    override fun stopGeneration() {
        if (handle != 0L) LlamaNative.stopGenerate(handle)
    }

    override fun close() {
        if (handle != 0L) {
            LlamaNative.freeModel(handle)
            handle = 0L
        }
    }

    /**
     * system 前置 + 历史 + 最新用户输入。
     *
     * 与文本引擎不同，这里**始终**把 [prompt] 作为最后一条 user 消息追加：视觉请求的
     * prompt 里含有 media marker，必须与 images 一一对应，不能因为历史里已有 user 消息
     * 就被静默丢弃。
     */
    private fun buildMessageList(session: ChatSession, prompt: String): List<ChatMessage> = buildList {
        if (!session.systemPrompt.isNullOrBlank()) {
            add(ChatMessage(role = Role.System, content = session.systemPrompt))
        }
        addAll(session.messages)
        if (prompt.isNotBlank()) {
            add(ChatMessage(role = Role.User, content = prompt))
        } else if (none { it.role == Role.User }) {
            add(ChatMessage(role = Role.User, content = "请描述这张图片。"))
        }
    }

    companion object {
        private const val TAG = "AndroidVisionEngine"
        private const val N_BATCH = 512
    }
}
