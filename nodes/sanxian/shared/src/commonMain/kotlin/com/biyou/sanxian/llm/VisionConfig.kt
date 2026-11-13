package com.biyou.sanxian.llm

/**
 * 视觉推理配置。
 *
 * 与 [LlmConfig] 分离：视觉引擎是独立句柄（自己的 LLM + mmproj），
 * 与常驻 chat 模型互不影响；未配置时节点不承接 vision/ocr 请求。
 */
data class VisionConfig(
    /** 视觉 LLM（如 Qwen2.5-VL-3B-Instruct Q4_K_M）的绝对路径 */
    val modelPath: String = "",
    /** mmproj（视觉投影权重）的绝对路径 */
    val mmprojPath: String = "",
    val contextSize: Int = 4096,
    val threads: Int = 4,
    val temperature: Float = 0.2f,
    val topP: Float = 0.9f,
    val topK: Int = 40,
    val repeatPenalty: Float = 1.05f,
    val repeatLastN: Int = 64,
    val seed: Long = 0L,
    val maxTokens: Int = 512,
    /** 动态分辨率模型的图像 token 上限；越大越清晰也越慢越占内存。512 为真机实测默认值。 */
    val imageMaxTokens: Int = 512,
    val useMmap: Boolean = true,
    val kvCacheType: KvCacheType = KvCacheType.F16,
)
