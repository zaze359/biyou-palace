package com.smallphone.llm

import kotlinx.coroutines.flow.Flow

/**
 * 统一的端侧大模型推理引擎接口（KMP commonMain 定义）。
 * 各平台通过 expect/actual 提供实现：
 *  - Android: [com.smallphone.llm.AndroidLlmEngine]（JNI → libllama_jni.so）
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
     */
    fun generate(session: ChatSession, prompt: String): Flow<String>

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

/** 平台工厂：由 actual 在 androidMain / iosMain 提供具体实现 */
expect fun createLlmEngine(config: LlmConfig): LlmEngine
