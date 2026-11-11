package com.biyou.sanxian.node

import com.biyou.sanxian.contract.OpenAIError
import com.biyou.sanxian.contract.OpenAIErrorBody
import kotlinx.serialization.json.Json

/**
 * OpenAI 风格错误（契约要求统一格式）。与准圣 [errors.py] 同构。
 *
 * HTTP 状态码由调用方设定（见 [status]），JSON 体恒为
 * `{"error":{"message":...,"type":...,"param":null,"code":...}}`。
 */
class NodeException(
    message: String,
    val status: Int,
    val type: String = "invalid_request_error",
    val code: String,
    /** 仅 429 / 降载 503 等需要提示重试时设置；StatusPages 据此写 Retry-After。 */
    val retryAfterSeconds: Int? = null,
) : Exception(message)

/** 上游不可用（引擎层异常 / 模型未加载）→ 503 */
fun upstreamUnavailable(message: String) =
    NodeException(message, status = 503, type = "server_error", code = "upstream_unavailable")

/** 并发超限 → 429（响应头另加 Retry-After） */
fun concurrencyLimitExceeded(message: String) =
    NodeException(message, status = 429, type = "invalid_request_error", code = "rate_limit_exceeded")

/** 能力不支持 → 501 */
fun capabilityNotSupported(message: String) =
    NodeException(message, status = 501, type = "invalid_request_error", code = "capability_not_supported")

/** 无效请求（参数缺失等）→ 400 */
fun badRequest(message: String) =
    NodeException(message, status = 400, type = "invalid_request_error", code = "invalid_parameter")

/** 鉴权失败 → 401 */
fun unauthorized(message: String) =
    NodeException(message, status = 401, type = "invalid_request_error", code = "invalid_api_key")

/**
 * 发热降载 → 503。code 固定为 `thermal_shedding`，响应头带 `Retry-After`。
 * 与 429（并发已满）区分：降载是设备保护，需要比单纯限流更长的退避时间。
 */
fun thermalShedding(message: String, retryAfterSeconds: Int) =
    NodeException(
        message,
        status = 503,
        type = "server_error",
        code = "thermal_shedding",
        retryAfterSeconds = retryAfterSeconds,
    )

private val json = Json { encodeDefaults = true }

/** 把 [NodeException] 转为 (HTTP 状态码, JSON 字符串) */
fun NodeException.toOpenAIError(): Pair<Int, String> {
    val body = OpenAIError(
        OpenAIErrorBody(message = message ?: "", type = type, param = null, code = code)
    )
    return status to json.encodeToString(OpenAIError.serializer(), body)
}
