package com.biyou.sanxian.ui

/**
 * 模型存储相关平台契约（composeApp commonMain 定义，androidMain 实现）。
 *
 * 放在 app 层而非 shared 的 [com.biyou.sanxian.llm] 包：取得模型落盘目录需要
 * `Application` 上下文，而该上下文属于 composeApp（[com.biyou.sanxian.SanXianApplication]），
 * shared 模块无法反向依赖 composeApp，故此处就近声明 expect/actual。
 */

/**
 * 模型目录所在文件系统的存储统计。
 * @param freeBytes 可用字节（Android：[android.os.StatFs.availableBytes]）
 * @param totalBytes 总字节（Android：[android.os.StatFs.totalBytes]）
 */
data class ModelsStorageInfo(
    val freeBytes: Long,
    val totalBytes: Long,
)

/**
 * 模型落盘根目录（应用私有，无需存储权限）。
 * androidMain 实现取 `Application` 上下文的
 * `getExternalFilesDir(DIRECTORY_DOWNLOADS)/models`，不存在时自动创建。
 */
expect fun getModelsDirectory(): String

/** 模型目录所在文件系统的可用/总字节（androidMain 用 [android.os.StatFs]） */
expect fun getModelsStorageInfo(): ModelsStorageInfo

/**
 * 扫描模型目录下的已下载模型（忽略 `.part` 临时文件）。
 * androidMain 用 [java.io.File] 实现；返回 [LocalModel] 列表（可能为空）。
 */
expect fun scanLocalModels(): List<LocalModel>

/** 删除指定路径的本地模型文件；成功返回 true。androidMain 用 [java.io.File] 实现。 */
expect fun deleteModelFile(path: String): Boolean
