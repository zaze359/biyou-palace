package com.biyou.sanxian.llm

/**
 * 原生层向 Kotlin 逐 token 推送生成结果。
 *
 * 注意：不能声明为 `internal`——Kotlin 会对 internal 函数做名称修饰，
 * 导致 JNI 侧 `GetMethodID(..., "onToken", ...)` 查找失败。
 *
 * @return true 继续生成；false 请求中断
 */
fun interface TokenSink {
    fun onToken(token: String): Boolean
}

/**
 * libllama_jni.so 的 JNI 声明。
 *
 * 类名与包名必须和 nativelib/src/main/cpp/jni_bridge.cpp 中的
 * `Java_com_biyou_sanxian_llm_LlamaNative_xxx` 函数名保持一致。
 */
object LlamaNative {

    init {
        System.loadLibrary("llama_jni")
    }

    /** 探测 llama.cpp 后端能力，用于确认原生库已正确链接。 */
    external fun hello(): String

    /**
     * 加载 GGUF 模型。
     * @return 原生会话句柄；加载失败返回 0
     */
    external fun loadModel(
        path: String,
        nCtx: Int,
        nThreads: Int,
        nBatch: Int,
        nGpuLayers: Int,
        useMmap: Boolean,
        kvCacheType: Int,
        temperature: Float,
        topP: Float,
        enableEmbeddings: Boolean,
    ): Long

    /**
     * 运行时调整采样参数并重建原生采样链（D4：top-k / 重复惩罚 / 可控 seed）。
     * 在 [loadModel] 之后调用以应用完整 [com.biyou.sanxian.llm.LlmConfig]；
     * 也可在生成过程中动态调用以实时切换采样策略。
     *
     * @param topK       top-k 候选截断；<=0 禁用
     * @param repeatPenalty 重复惩罚强度（penalty_repeat）；<=1.0 关闭
     * @param repeatLastN   重复惩罚回溯窗口（penalty_last_n）
     * @param seed      采样种子；>0 决定论，<=0 每次生成随机
     */
    external fun setSamplingParams(
        handle: Long,
        temperature: Float,
        topP: Float,
        topK: Int,
        repeatPenalty: Float,
        repeatLastN: Int,
        seed: Long,
    )

    /** 释放模型与全部原生资源。 */
    external fun freeModel(handle: Long)

    /** 请求中断当前生成。 */
    external fun stopGenerate(handle: Long)

    /** 清空 KV Cache（开启新会话时使用）。 */
    external fun clearContext(handle: Long)

    /** 模型描述（架构等）。 */
    external fun modelDesc(handle: Long): String

    /** 模型文件字节数。 */
    external fun modelSize(handle: Long): Long

    /** 模型参数量。 */
    external fun modelParamCount(handle: Long): Long

    /**
     * 流式生成：把多轮消息交给原生层，由原生在一次调用内完成
     * 「输入 token 预算 → 按消息裁剪 → chat template 渲染 → 分词 → eval → 采样」，
     * 每产出一个 token 回调 [TokenSink.onToken]。
     *
     * 为什么传消息数组而不是渲染好的 prompt 字符串：上下文超出 `n_ctx` 时必须按**消息**粒度裁剪
     * （丢最早的历史、保全 system 与最新提问），而裁剪必须发生在 chat template 渲染**之前**
     * —— 渲染后的 token 流里 `<|im_start|>` 这类标记横跨多个 token，按 token 裸切会切出非法 prompt。
     * 而「渲染 + 分词 + 判预算」只有在原生侧才能闭环（`n_ctx` / `maxTokens` / 分词器都在那里）。
     *
     * 原生会在模型未内嵌 chat template、或模板不在 llama.cpp 预置模板族内时退化为 ChatML。
     *
     * @param roles 与 [contents] 等长平行数组，元素为 `system` / `user` / `assistant`
     * @return >=0 生成的 token 数；<0 为错误码
     *         （-1 未加载 / -2 回调缺失 / -3 已取消 / -4 推理失败 / -5 回调异常 / -6 无法构造 prompt）
     */
    external fun generate(
        handle: Long,
        roles: Array<String>,
        contents: Array<String>,
        maxTokens: Int,
        sink: TokenSink,
    ): Int

    /**
     * 只渲染不推理：用模型 GGUF 内嵌的 chat template（`tokenizer.chat_template`）把多轮消息格式化为 prompt。
     *
     * **诊断入口**——生产生成路径走 [generate]（它内部用同一个渲染器，且额外做了预算与裁剪）。
     * 最典型的用途是免推理验证模板取源：拿一个非 ChatML 系的模型（如 Llama-3.2-Instruct）
     * 调一次，看渲染结果里是不是自家标记（`<|start_header_id|>`）而不是 `<|im_start|>`。
     *
     * @return 渲染后的 prompt；消息数组为空时返回空串
     */
    external fun formatChat(handle: Long, roles: Array<String>, contents: Array<String>): String

    /**
     * 模型 GGUF 内嵌的 chat template 原文，供诊断 / UI 展示当前实际使用的模板。
     * @return 模板串；模型未内嵌时返回空串
     */
    external fun modelChatTemplate(handle: Long): String

    /**
     * 文本向量化（embeddings JNI 桥，Plan A：独立 embedding 句柄）。
     *
     * 调用方应传入**独立加载且开启 embeddings 的句柄**（loadModel 时 enableEmbeddings=true），
     * 与 chat 句柄隔离，避免 llama_encode / llama_decode 互相污染 KV。
     *
     * @param handle 开启 embeddings 的上下文句柄
     * @param text   待向量化文本（空串会被原生层以空格兜底，保持维度一致）
     * @return n_embd 维向量；失败返回长度 0 的 FloatArray
     */
    external fun embed(handle: Long, text: String): FloatArray
}
