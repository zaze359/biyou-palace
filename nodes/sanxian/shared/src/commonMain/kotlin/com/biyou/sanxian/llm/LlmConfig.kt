package com.biyou.sanxian.llm

/**
 * KV Cache 张量精度类型。
 *
 * 映射到 llama.cpp 的 [ggml_type]（整型值取自本仓库
 * `nativelib/.../third_party/llama.cpp/ggml/include/ggml.h` 的实际枚举定义）：
 *  - F16   = GGML_TYPE_F16  (1)  默认，精度/占用平衡
 *  - F32   = GGML_TYPE_F32  (0)  最高精度，占用最大
 *  - Q8_0  = GGML_TYPE_Q8_0 (8)  8-bit 量化，KV 内存较 F16 约减半
 *  - Q4_0  = GGML_TYPE_Q4_0 (2)  4-bit 量化，最大压缩，质量风险较高
 *
 * [nativeValue] 直接透传给原生层 `loadModel`，由 jni_bridge.cpp 经
 * `static_cast<ggml_type>` 写入 `llama_context_params.type_k/type_v`。
 */
enum class KvCacheType(val nativeValue: Int) {
    F16(1),
    F32(0),
    Q8_0(8),
    Q4_0(2),
}

/**
 * 大语言模型引擎配置。
 * Task #1 仅使用基础字段；Phase 3 的 GPU 加速 / mmap / KV Cache 精度等字段已逐步填充。
 */
data class LlmConfig(
    /** GGUF 模型文件在设备上的绝对路径（Phase 3 由模型管理器提供） */
    val modelPath: String = "",
    /** 上下文窗口 token 数 */
    val contextSize: Int = 2048,
    /** 推理线程数，默认 4（Phase 3 按设备核数/温度动态调整） */
    val threads: Int = 4,
    /** 采样温度 */
    val temperature: Float = 0.7f,
    /** top-p 采样 */
    val topP: Float = 0.9f,
    /** top-k 采样；<=0 表示禁用（候选集不截断） */
    val topK: Int = 40,
    /** 重复惩罚强度（penalty_repeat）；<=1.0 表示关闭 */
    val repeatPenalty: Float = 1.1f,
    /** 重复惩罚回溯窗口（penalty_last_n）：仅对最近 N 个 token 施加惩罚 */
    val repeatLastN: Int = 64,
    /** 采样种子；>0 决定论复现（同一 prompt + 同一 seed 产出一致），<=0 每次生成随机（默认） */
    val seed: Long = 0L,
    /** 单次生成最大 token 数 */
    val maxTokens: Int = 512,
    // ---- Phase 3 扩展字段 ----
    /** 卸载到 GPU 的层数（0 = 纯 CPU）。Android: Vulkan/OpenCL/NNAPI；iOS: Metal */
    val gpuLayers: Int = 0,
    /** 是否使用 mmap 加载权重（默认开启，降低内存占用） */
    val useMmap: Boolean = true,
    /** KV Cache 张量精度（默认 F16，精度/占用平衡）。Q8_0/Q4_0 可降低内存受限设备的 KV 占用 */
    val kvCacheType: KvCacheType = KvCacheType.F16,
    /**
     * 独立 embedding 模型路径（Plan A：与 chat 模型分属两个上下文句柄）。
     * 非空时 AndroidLlmEngine 会在 loadModel 内额外加载该模型到 embedding 句柄，
     * 并开启 embeddings（cparams.embeddings=true + pooling_type=MEAN）。
     * 为空则不加载 embedding 模型，节点不承接 embedding 请求（/v1/embeddings 走 501/503）。
     */
    val embeddingModelPath: String? = null,
)
