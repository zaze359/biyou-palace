package com.biyou.sanxian.llm

/**
 * 推理引擎相关的异常类型，统一在 commonMain 定义，
 * 各平台 actual 实现抛出对应子类，UI 层据此展示错误。
 */
sealed class LlmError(
    message: String? = null,
    cause: Throwable? = null,
) : Throwable(message, cause) {

    /** 模型文件在指定路径不存在 */
    class ModelNotFound(path: String) : LlmError("模型文件不存在: $path")

    /** 模型加载失败（格式错误 / 内存不足 / 权限等） */
    class LoadFailed(reason: String) : LlmError("模型加载失败: $reason")

    /** 推理过程失败 */
    class GenerateFailed(reason: String) : LlmError("推理失败: $reason")

    /** 未加载模型就发起推理 */
    class NotLoaded : LlmError("引擎尚未加载模型")

    /** 用户主动停止生成 */
    class Cancelled : LlmError("已取消生成")
}
