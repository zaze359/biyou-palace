package com.biyou.sanxian.ui

import android.os.Environment
import android.os.StatFs
import com.biyou.sanxian.SanXianApplication
import java.io.File

/**
 * Android 平台 [getModelsDirectory] / [getModelsStorageInfo] 实现。
 *
 * 证据：与 `composeApp/src/commonMain/kotlin/com/biyou/sanxian/ui/ModelsStorage.kt` 的 expect 声明一一对应；
 * 通过 [com.biyou.sanxian.SanXianApplication.appContext] 取应用私有外部存储目录，避免依赖运行时权限。
 */
actual fun getModelsDirectory(): String {
    val base = SanXianApplication.appContext.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
        ?: SanXianApplication.appContext.filesDir
    return File(base, "models").apply { if (!exists()) mkdirs() }.absolutePath
}

actual fun getModelsStorageInfo(): ModelsStorageInfo {
    val stat = StatFs(getModelsDirectory())
    return ModelsStorageInfo(freeBytes = stat.availableBytes, totalBytes = stat.totalBytes)
}

actual fun scanLocalModels(): List<LocalModel> {
    val dir = File(getModelsDirectory())
    if (!dir.isDirectory) return emptyList()
    return dir.listFiles()
        ?.filter { it.isFile && it.name.endsWith(".gguf") }
        ?.map { LocalModel(fileName = it.name, path = it.absolutePath, sizeBytes = it.length()) }
        ?: emptyList()
}

actual fun deleteModelFile(path: String): Boolean =
    runCatching { File(path).delete() }.getOrDefault(false)
