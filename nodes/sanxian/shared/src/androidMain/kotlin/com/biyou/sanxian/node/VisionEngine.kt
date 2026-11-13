package com.biyou.sanxian.node

import com.biyou.sanxian.llm.ChatSession
import com.biyou.sanxian.llm.GenerateStats
import kotlinx.coroutines.flow.Flow

/**
 * 视觉推理后端抽象（vision / OCR 共用同一套 LLM + mmproj 权重）。
 *
 * 与 [EmbeddingEngine] 同理：服务层只依赖抽象，便于 JVM 端点级测试注入假实现；
 * 真实实现在 androidMain 的 `AndroidVisionEngine`（JNI → llama.cpp `tools/mtmd`）。
 *
 * OCR 不是独立模型：调用方用提示词要求「识别图中文字」即可，权重与 vision 完全共享。
 */
interface VisionEngine {
    /** 视觉通道是否真正就绪（mmproj 加载成功且模型支持 vision）。 */
    val isLoaded: Boolean

    /** 提示词中代表一张图的占位符，必须与原生 mtmd marker 一致。 */
    val mediaMarker: String

    /**
     * 多模态流式生成。[images] 按序对应 [prompt]（及历史消息）里的第 N 个 [mediaMarker]。
     *
     * @param prompt    最新一条用户输入（已把图像分片替换为 [mediaMarker]）
     * @param maxTokens 请求级生成长度上限；<=0 用引擎配置值
     */
    fun generate(
        session: ChatSession,
        prompt: String,
        images: List<ByteArray>,
        maxTokens: Int = 0,
        stats: GenerateStats? = null,
    ): Flow<String>

    /** 停止当前生成（图像编码阶段无法中断，编码结束后立即响应）。 */
    fun stopGeneration()

    /** 释放模型与原生资源。 */
    fun close()
}

/**
 * 视觉请求的客户端错误（图像超限 / 解码失败 / 文本+图像超出上下文）。
 * 路由层统一转 400 + OpenAI 错误体；与引擎不可用的 503 区分。
 */
class VisionRequestException(message: String) : Exception(message)

/**
 * 图像获取抽象：把 OpenAI `image_url`（http(s) 或 data URI）变成可直接送进原生解码器的字节。
 *
 * 生产实现负责下载、尺寸校验与缩放；测试注入假实现即可覆盖错误路径，
 * 不需要网络，也不触发 Android Bitmap API。
 */
interface VisionImageLoader {
    /** @throws NodeException(400) 地址非法、下载失败、超过体积上限或解码失败 */
    suspend fun load(url: String): ByteArray
}

/** 默认加载器：未显式注入时（例如测试只关心能力门控）拒绝所有图像地址。 */
object UnsupportedVisionImageLoader : VisionImageLoader {
    override suspend fun load(url: String): ByteArray =
        throw capabilityNotSupported("该节点未启用图像加载")
}
