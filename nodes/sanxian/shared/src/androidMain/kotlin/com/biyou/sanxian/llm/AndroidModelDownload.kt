package com.biyou.sanxian.llm

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * Android 平台 [ModelDownloadManager] 实现：基于标准 [HttpURLConnection] 从
 * HuggingFace（或任意支持 Range 的服务端）下载 GGUF，断点续传、进度回报、可取消。
 *
 * 设计要点（对照规范 [com.biyou.sanxian.llm.LlmEngine] 的 actual 写法）：
 *  - 与 [AndroidLlmEngine] 一样走 `actual fun createModelDownloadManager()` 工厂；
 *  - 下载为阻塞 IO，包裹进 `flow { }` 并在 [Dispatchers.IO] 上运行，逐块 emit 进度；
 *  - 收集方取消时 `currentCoroutineContext().ensureActive()` 抛异常，循环退出，
 *    已落盘的 `<localPath>.part` 保留以便下次续传。
 *
 * 证据：本文件与 `shared/src/commonMain/kotlin/com/biyou/sanxian/llm/ModelDownload.kt` 的
 * expect 声明一一对应；不引入任何新依赖（HttpURLConnection 为 JDK 标准库）。
 */
actual fun createModelDownloadManager(): ModelDownloadManager = AndroidModelDownloadManager()

class AndroidModelDownloadManager : ModelDownloadManager {

    override fun download(model: RemoteModel): Flow<DownloadState> = flow {
        emit(DownloadState.Progress(0, model.sizeBytes ?: -1, 0))

        try {
            val target = File(model.localPath)
            val part = File(model.localPath + ".part")

            // 续传起点：已有 .part 文件则从其长度处续传
            val start = if (part.exists()) part.length() else 0L

            val (conn, append) = openWithRedirects(model.url, start)
            val code = conn.responseCode

            // 200 = 服务端忽略 Range，从头覆盖；206 = 支持 Range，续传
            val contentLength = conn.contentLengthLong
            val total = if (append) {
                if (contentLength > 0) contentLength + start else (model.sizeBytes ?: -1)
            } else {
                if (contentLength > 0) contentLength else (model.sizeBytes ?: -1)
            }

            if (code !in 200..299) {
                conn.disconnect()
                emit(DownloadState.Error("HTTP $code: ${conn.responseMessage ?: ""}"))
                return@flow
            }

            // 不支持续传时丢弃旧 .part，从 0 覆盖
            val outFile = if (append) part else run { part.delete(); part }
            var written = if (append) start else 0L
            val buffer = ByteArray(BUFFER)
            val startedAt = System.currentTimeMillis()

            try {
                conn.inputStream.use { input ->
                    outFile.outputStream().use { output ->
                        var read: Int
                        while (input.read(buffer).also { read = it } >= 0) {
                            currentCoroutineContext().ensureActive()
                            output.write(buffer, 0, read)
                            written += read
                            val elapsed = (System.currentTimeMillis() - startedAt).coerceAtLeast(1)
                            val speed = written * 1000 / elapsed
                            emit(DownloadState.Progress(written, total, speed))
                        }
                    }
                }
            } catch (e: IOException) {
                conn.disconnect()
                emit(DownloadState.Error("下载中断：${e.message ?: "IO"}（已写入 ${written}B，可续传）"))
                return@flow
            }

            conn.disconnect()

            // 完成后把 .part 重命名为最终文件；跨存储分区重命名失败时退化为拷贝
            if (!outFile.renameTo(target)) {
                try {
                    target.outputStream().use { o -> outFile.inputStream().use { i -> i.copyTo(o) } }
                    outFile.delete()
                } catch (e: IOException) {
                    emit(DownloadState.Error("完成时文件移动失败：${e.message ?: "IO"}"))
                    return@flow
                }
            }
            emit(DownloadState.Completed(model.localPath))
            Log.i(TAG, "模型下载完成：${model.localPath} ($written B)")
        } catch (e: CancellationException) {
            throw e
        } catch (e: Throwable) {
            // 网络/连接类异常（如内网无法访问 HuggingFace 的 Connection reset）一律降级为错误态，
            // 不再向上抛导致主线程崩溃。已写入的 .part 保留，下次可续传。
            Log.e(TAG, "模型下载失败（已降级为错误态）：${model.url}", e)
            emit(DownloadState.Error("下载失败：${e.message ?: e.javaClass.simpleName}（可重试）"))
        }
    }.flowOn(Dispatchers.IO)

    /**
     * 打开下载连接并跟随重定向，续传时保留 Range 头。
     * @return 最终连接与是否以续传（206）方式响应。
     */
    private fun openWithRedirects(initialUrl: String, start: Long): Pair<HttpURLConnection, Boolean> {
        var url = initialUrl
        repeat(MAX_REDIRECTS) {
            val conn = (URL(url).openConnection() as HttpURLConnection).apply {
                connectTimeout = CONNECT_TIMEOUT
                readTimeout = 0
                instanceFollowRedirects = false
                if (start > 0) setRequestProperty("Range", "bytes=$start-")
                connect()
            }
            val code = conn.responseCode
            if (code in 301..399) {
                val loc = conn.getHeaderField("Location")
                conn.disconnect()
                if (loc.isNullOrBlank()) throw IOException("重定向缺少 Location 头")
                url = loc
                return@repeat
            }
            return conn to (code == HttpURLConnection.HTTP_PARTIAL)
        }
        throw IOException("重定向次数超过上限（$MAX_REDIRECTS）")
    }

    private companion object {
        const val TAG = "AndroidModelDownloadManager"
        const val BUFFER = 32 * 1024
        const val CONNECT_TIMEOUT = 15_000
        const val MAX_REDIRECTS = 5
    }
}
