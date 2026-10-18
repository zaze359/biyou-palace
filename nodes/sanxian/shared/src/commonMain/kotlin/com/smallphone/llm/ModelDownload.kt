package com.smallphone.llm

import kotlinx.coroutines.flow.Flow

/**
 * 一次模型下载的进度/结果状态。
 *
 * 证据：`shared/src/commonMain/kotlin/com/smallphone/llm/ModelDownload.kt`
 * （与 [LlmConfig]、[LlmEngine] 同包，遵循该模块 expect/actual 分层约定）。
 */
sealed interface DownloadState {
    /** 空闲（尚未开始） */
    data object Idle : DownloadState

    /**
     * 下载进行中。
     * @param downloadedBytes 已写入字节（含断点续传的起点）
     * @param totalBytes 目标总字节；服务端未返回 Content-Length 时为 -1
     * @param bytesPerSecond 近似瞬时速度（B/s）
     */
    data class Progress(
        val downloadedBytes: Long,
        val totalBytes: Long,
        val bytesPerSecond: Long,
    ) : DownloadState

    /** 下载完成，[localPath] 为最终文件绝对路径 */
    data class Completed(val localPath: String) : DownloadState

    /** 下载失败（含被用户取消/中断） */
    data class Error(val message: String) : DownloadState
}

/**
 * 远端 GGUF 模型的描述。
 *
 * 证据：`shared/src/commonMain/kotlin/com/smallphone/llm/ModelDownload.kt`
 */
data class RemoteModel(
    /** HuggingFace 仓库 id，如 "Qwen/Qwen2.5-0.5B-Instruct-GGUF" */
    val repoId: String,
    /** 仓库内文件名，如 "qwen2.5-0.5b-instruct-q4_k_m.gguf" */
    val fileName: String,
    /** 目标总字节；未知传 null（UI 进度退化为不确定态） */
    val sizeBytes: Long? = null,
    /** 完整下载 URL（HuggingFace resolve 地址）；也可经 [huggingFace] 构造 */
    val url: String,
    /** 设备上的目标绝对路径；download() 先写 `<localPath>.part`，完成后重命名 */
    val localPath: String,
) {
    companion object {
        /** 构造 HuggingFace resolve 下载地址（默认 main 分支） */
        fun huggingFace(
            repoId: String,
            fileName: String,
            localPath: String,
            branch: String = "main",
        ): RemoteModel = RemoteModel(
            repoId = repoId,
            fileName = fileName,
            url = "https://huggingface.co/$repoId/resolve/$branch/$fileName",
            localPath = localPath,
        )
    }
}

/**
 * 跨平台模型下载管理器契约（commonMain 定义，androidMain 实现）。
 *
 * 支持断点续传：若 `<localPath>.part` 已存在，将从该文件当前长度处续传；
 * 收集方取消时下载中断，已写入的 `.part` 保留，下次 download() 自动续传。
 *
 * 证据：本接口与 [createModelDownloadManager] 的 expect/actual 机制，
 * 与 [LlmEngine] / [createLlmEngine] 完全对齐（`shared/src/androidMain/.../AndroidLlmEngine.kt`）。
 */
interface ModelDownloadManager {
    /**
     * 下载 [model]，以 [Flow] 回报进度。
     * 收集方取消（退出界面 / ViewModel 清理）即中断下载；已落盘的 `.part` 不删除。
     */
    fun download(model: RemoteModel): Flow<DownloadState>
}

/** 平台工厂：由 actual 在 androidMain 提供具体实现 */
expect fun createModelDownloadManager(): ModelDownloadManager
