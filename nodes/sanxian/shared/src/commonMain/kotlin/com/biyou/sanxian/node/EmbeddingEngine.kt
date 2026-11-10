package com.biyou.sanxian.node

/**
 * 向量化后端抽象（散仙主承接方）。
 *
 * 散仙用 llama.cpp 做真实 embedding（vendored llama.cpp 已原生支持）。该能力通过新增
 * JNI 桥注入（见 `NATIVE_INTEGRATION.md`），`AndroidLlmEngine` 实现本接口；服务层只依赖
 * 此抽象，不直接耦合 fork 的 `LlmEngine`，从而保持 commonMain 可独立编译/单测。
 *
 * 若节点未声明 embedding 能力，或后端未就绪，服务层直接 501，不调用本接口。
 */
interface EmbeddingEngine {
    /**
     * embedding 句柄是否已真正加载。
     *
     * `/v1/health` 的 `models[].loaded` 必须反映真实句柄状态，不能只看配置里有没有声明，
     * 否则「配置了但加载失败」会被误报为可用，上层路由会把请求打到必然 503 的节点。
     */
    val isLoaded: Boolean

    /**
     * 批量向量化；返回与 [texts] 等长的向量列表。
     *
     * suspend：实现方需在引擎层串行化 native 调用（同一 embedding 句柄不能并发 encode）。
     */
    suspend fun embed(texts: List<String>): EmbeddingResult
}

/**
 * 一次 embedding 调用的结果。
 *
 * [promptTokens] 为实际 tokenize（并按下限截断）后的总 token 数，供 usage.prompt_tokens；
 * 不是字符数，也不包含被 n_ubatch 截掉的部分。
 */
data class EmbeddingResult(
    val vectors: List<List<Float>>,
    val promptTokens: Int,
)
