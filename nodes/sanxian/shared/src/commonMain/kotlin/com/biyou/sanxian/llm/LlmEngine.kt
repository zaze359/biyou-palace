package com.biyou.sanxian.llm

import kotlinx.coroutines.flow.Flow

/**
 * 统一的端侧大模型推理引擎接口（KMP commonMain 定义）。
 * 各平台通过 expect/actual 提供实现：
 *  - Android: [com.biyou.sanxian.llm.AndroidLlmEngine]（JNI → libllama_jni.so）
 *  - iOS (Phase 2): IosLlmEngine（cinterop → llama.xcframework）
 */
interface LlmEngine {

    /** 加载模型；modelPath 为空时应抛出 [LlmError.ModelNotFound] */
    fun loadModel(config: LlmConfig)

    /** 是否已加载模型 */
    fun isModelLoaded(): Boolean

    /**
     * 基于会话与最新用户输入流式生成回复。
     * 每产生一个 token（或字符块）就 emit 一次，便于 UI 逐字渲染。
     *
     * @param maxTokens 请求级生成长度上限；<=0 时用引擎配置值，正数会被夹到配置上限。
     * @param stats     可选：生成结束后回填真实 token 数（prompt 为**裁剪后**的实际值），
     *                  供 `/v1/chat/completions` 的 usage 使用。
     */
    fun generate(
        session: ChatSession,
        prompt: String,
        maxTokens: Int = 0,
        stats: GenerateStats? = null,
    ): Flow<String>

    /** 停止当前生成（下一次 emit 前应抛出 [LlmError.Cancelled] 或自然结束） */
    fun stopGeneration()

    /** 已加载模型的元信息；未加载返回 null */
    fun getModelInfo(): ModelInfo?

    /** 释放模型与资源 */
    fun close()
}

data class ModelInfo(
    val name: String,
    val contextSize: Int,
    val backend: String,
)

/**
 * 单次生成的 token 统计。
 *
 * [promptTokens] 是**裁剪后**真正送入 KV 的 token 数（不是原始输入长度），因此触发上下文
 * 截断时会小于客户端原始请求的 token 数——这正是 usage 需要如实反映的口径。
 * 生成被取消（客户端断开）时可能保持 0，调用方不应把 0 当成合法用量。
 */
class GenerateStats {
    var promptTokens: Int = 0
    var completionTokens: Int = 0
}

/** 平台工厂：由 actual 在 androidMain / iosMain 提供具体实现 */
expect fun createLlmEngine(config: LlmConfig): LlmEngine
