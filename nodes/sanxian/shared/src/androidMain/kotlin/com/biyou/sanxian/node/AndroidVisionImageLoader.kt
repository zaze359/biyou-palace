package com.biyou.sanxian.node

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * 生产图像加载器：下载 → 体积/尺寸校验 → 缩放到最长边 [MAX_EDGE] → JPEG 重编码。
 *
 * 缩小发生在进原生层之前，避免超大图让 stb 解码时一次性申请数百 MB；
 * 端侧视觉编码本身也会把分辨率压到模型 patch 网格，1024 已足够。
 */
class AndroidVisionImageLoader : VisionImageLoader {

    override suspend fun load(url: String): ByteArray = withContext(Dispatchers.IO) {
        val raw = when {
            url.startsWith("data:", ignoreCase = true) -> decodeDataUrl(url)
            url.startsWith("http://", ignoreCase = true) ||
                url.startsWith("https://", ignoreCase = true) -> download(url)
            else -> throw badRequest("不支持的图像地址：仅支持 http(s) 与 data:image/...;base64")
        }
        if (raw.size > MAX_BYTES) {
            throw badRequest("图像过大：${raw.size / 1024} KB，上限 ${MAX_BYTES / 1024} KB")
        }
        downscale(raw)
    }

    private fun decodeDataUrl(url: String): ByteArray {
        val comma = url.indexOf(',')
        if (comma <= 0) throw badRequest("data URI 格式错误：缺少逗号分隔的元数据")
        val meta = url.substring(0, comma)
        if (!meta.startsWith("data:image/", ignoreCase = true)) {
            throw badRequest("data URI 仅支持 image/*")
        }
        if (!meta.contains(";base64", ignoreCase = true)) {
            throw badRequest("data URI 仅支持 base64 编码")
        }
        return runCatching { Base64.decode(url.substring(comma + 1), Base64.DEFAULT) }
            .getOrElse { throw badRequest("图像 base64 解码失败") }
    }

    private fun download(url: String): ByteArray {
        val conn = runCatching { URL(url).openConnection() as HttpURLConnection }
            .getOrElse { throw badRequest("图像 URL 非法：${it.message}") }
        try {
            conn.connectTimeout = CONNECT_TIMEOUT_MS
            conn.readTimeout = READ_TIMEOUT_MS
            conn.instanceFollowRedirects = true
            val code = runCatching { conn.responseCode }
                .getOrElse { throw badRequest("图像下载失败：${it.message ?: "网络错误"}") }
            if (code !in 200..299) throw badRequest("图像下载失败：HTTP $code")
            if (conn.contentLengthLong > MAX_BYTES) {
                throw badRequest("图像过大：${conn.contentLengthLong / 1024} KB，上限 ${MAX_BYTES / 1024} KB")
            }
            val out = ByteArrayOutputStream()
            conn.inputStream.use { input ->
                val buf = ByteArray(64 * 1024)
                var total = 0
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    total += n
                    if (total > MAX_BYTES) {
                        throw badRequest("图像过大：超过 ${MAX_BYTES / 1024} KB 上限")
                    }
                    out.write(buf, 0, n)
                }
            }
            return out.toByteArray()
        } catch (e: NodeException) {
            throw e
        } catch (e: IOException) {
            throw badRequest("图像下载失败：${e.message ?: e::class.java.simpleName}")
        } finally {
            conn.disconnect()
        }
    }

    private fun downscale(raw: ByteArray): ByteArray {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(raw, 0, raw.size, bounds)
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) {
            throw badRequest("图像解码失败：不支持的格式或文件已损坏")
        }

        val longest = maxOf(bounds.outWidth, bounds.outHeight)
        // 先按 2 的幂采样解码，防止超大图在 decode 阶段就 OOM
        var sample = 1
        while (longest / sample > MAX_EDGE * 2) sample *= 2

        val opts = BitmapFactory.Options().apply { inSampleSize = sample }
        val decoded = runCatching { BitmapFactory.decodeByteArray(raw, 0, raw.size, opts) }
            .getOrNull() ?: throw badRequest("图像解码失败：不支持的格式或内存不足")

        val scaled = try {
            val edge = maxOf(decoded.width, decoded.height)
            if (edge <= MAX_EDGE) {
                decoded
            } else {
                val ratio = MAX_EDGE.toFloat() / edge
                val w = (decoded.width * ratio).toInt().coerceAtLeast(1)
                val h = (decoded.height * ratio).toInt().coerceAtLeast(1)
                Bitmap.createScaledBitmap(decoded, w, h, true).also {
                    if (it !== decoded) decoded.recycle()
                }
            }
        } catch (e: OutOfMemoryError) {
            decoded.recycle()
            throw badRequest("图像解码失败：内存不足")
        }

        return try {
            ByteArrayOutputStream().use { out ->
                scaled.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out)
                out.toByteArray()
            }
        } finally {
            scaled.recycle()
        }
    }

    companion object {
        /** 单图编码字节上限；超过直接 400，不尝试解码。 */
        const val MAX_BYTES = 4 * 1024 * 1024

        /** 缩放后最长边（像素）。 */
        const val MAX_EDGE = 1024

        private const val JPEG_QUALITY = 85
        private const val CONNECT_TIMEOUT_MS = 5_000
        private const val READ_TIMEOUT_MS = 15_000
    }
}
