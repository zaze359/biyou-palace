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
     * 批量向量化；返回与 [texts] 等长的向量列表。
     *
     * suspend：实现方需在引擎层串行化 native 调用（同一 embedding 句柄不能并发 encode）。
     */
    suspend fun embed(texts: List<String>): List<List<Float>>
}
