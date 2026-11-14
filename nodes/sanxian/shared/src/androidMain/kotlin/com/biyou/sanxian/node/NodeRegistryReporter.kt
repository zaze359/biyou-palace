package com.biyou.sanxian.node

import android.util.Log
import com.biyou.sanxian.contract.HealthResponse
import com.biyou.sanxian.contract.Limits
import com.biyou.sanxian.contract.ModelEntry
import com.biyou.sanxian.contract.NodeMeta
import com.biyou.sanxian.contract.Resources
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.encodeToJsonElement
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.NetworkInterface
import java.net.URL
import java.security.MessageDigest
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter
import java.util.Collections
import java.util.UUID
import kotlin.math.max
import kotlin.math.min

/**
 * 阵图注册报文的纯函数部分（可 JVM 单测）。
 *
 * `models_rev` 与准圣/Registry 逐字节一致：
 * `name|cap1,cap2|context_len` 按 name 排序后用 \n 连接，再 SHA-256。
 */
internal object RegistryPayload {
    private val json = Json { encodeDefaults = true }

    fun modelsRevision(models: List<ModelEntry>): String {
        val raw = models.sortedBy { it.name }.joinToString("\n") { model ->
            val caps = model.capabilities
                .map { it.lowercase() }
                .distinct()
                .sorted()
                .joinToString(",")
            "${model.name}|$caps|${model.contextLen ?: ""}"
        }
        val digest = MessageDigest.getInstance("SHA-256").digest(raw.toByteArray(Charsets.UTF_8))
        return "sha256:" + digest.joinToString("") { "%02x".format(it) }
    }

    fun registerBody(
        config: NodeConfig,
        health: HealthResponse,
        endpoint: String,
        bootId: String,
        sentAt: String,
    ): JsonObject = buildJsonObject {
        put("node", json.encodeToJsonElement(NodeMeta.serializer(), health.node))
        put("endpoint", endpoint)
        put("status", health.status)
        put("models", json.encodeToJsonElement(ListSerializer(ModelEntry.serializer()), health.models))
        put("limits", json.encodeToJsonElement(Limits.serializer(), health.limits))
        put("resources", json.encodeToJsonElement(Resources.serializer(), health.resources))
        put("heartbeat_interval_s", config.registryHeartbeatIntervalS.coerceIn(10, 120))
        put("boot_id", bootId)
        put("sent_at", sentAt)
    }

    fun heartbeatBody(
        health: HealthResponse,
        revision: String,
        bootId: String,
        sentAt: String,
    ): JsonObject = buildJsonObject {
        put("boot_id", bootId)
        put("status", health.status)
        put("models_rev", revision)
        put("loaded_models", buildJsonArray {
            health.models.filter { it.loaded }.forEach { add(it.name) }
        })
        put("resources", json.encodeToJsonElement(Resources.serializer(), health.resources))
        put("limits", json.encodeToJsonElement(Limits.serializer(), health.limits))
        put("sent_at", sentAt)
    }
}

internal class RegistryHttpException(val status: Int, val code: String) : Exception(code)

/**
 * 散仙 → 阵图注册表的出站 reporter。
 *
 * 硬约束与准圣一致：只有出站请求；后台协程；失败只改变 reporter 状态与日志，
 * 不影响前台服务、本地推理、/v1/health 与节点状态。
 */
class NodeRegistryReporter(
    private val config: NodeConfig,
    private val healthProvider: suspend () -> HealthResponse?,
) {
    private val json = Json { ignoreUnknownKeys = true }
    private val baseUrl = config.registryUrl?.trim()?.trimEnd('/').orEmpty()
    private val token = config.registryToken?.takeIf { it.isNotBlank() }
        ?: config.sharedToken?.takeIf { it.isNotBlank() }
    private val intervalMs = config.registryHeartbeatIntervalS.coerceIn(10, 120) * 1000L
    private val timeoutMs = config.registryTimeoutMs.coerceIn(500, 30_000).toInt()
    private val bootId = UUID.randomUUID().toString()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var job: Job? = null
    private var registered = false
    private var errorCount = 0
    private var lastLoggedError: String? = null

    @Volatile
    var state: String = if (baseUrl.isBlank()) "disabled" else "starting"
        private set

    @Volatile
    var lastOkAt: String? = null
        private set

    @Volatile
    var lastError: String? = null
        private set

    @Volatile
    var lastAttemptAt: String? = null
        private set

    @Volatile
    var attempts: Int = 0
        private set

    fun start() {
        if (baseUrl.isBlank()) {
            state = "disabled"
            return
        }
        if (token.isNullOrBlank()) {
            state = "missing_token"
            val message = "阵图注册已配置但 token 为空（registry_token / shared_token），注册不启动；本地服务不受影响"
            Log.e(TAG, message)
            NodeLogBuffer.record("WARNING", "registry", message)
            return
        }
        state = "starting"
        job = scope.launch { runLoop() }
    }

    /**
     * 停止上报。
     *
     * [deregister] 只在**用户显式关闭节点**时传 true；系统回收进程、app idle 停服务、
     * 服务重启等非用户意图的退出一律传 false，让阵图按租约自然过期显示 offline，
     * 避免把“被系统杀掉”误报成“优雅下线”。
     */
    fun stop(deregister: Boolean = false) {
        val wasRegistered = registered
        registered = false
        job?.cancel()
        job = null
        scope.cancel()
        if (deregister && wasRegistered && !token.isNullOrBlank() && baseUrl.isNotBlank()) {
            val body = buildJsonObject { put("boot_id", bootId) }.toString()
            Thread {
                runCatching {
                    runBlocking {
                        post("/${config.nodeId}/deregister", body, timeout = 1000)
                    }
                }
            }.apply { isDaemon = true }.start()
        }
        state = "stopped"
    }

    /** 执行一轮注册或心跳；测试可直接调用。 */
    suspend fun tick() {
        attempts += 1
        lastAttemptAt = nowIso()
        val health = healthProvider() ?: fallbackHealth()
        val revision = RegistryPayload.modelsRevision(health.models)
        try {
            if (!registered) {
                val body = RegistryPayload.registerBody(
                    config, health, endpoint(), bootId, nowIso(),
                )
                post("/register", body.toString())
                registered = true
            } else {
                val body = RegistryPayload.heartbeatBody(
                    health, revision, bootId, nowIso(),
                )
                val text = post("/${config.nodeId}/heartbeat", body.toString())
                val needSnapshot = runCatching {
                    json.parseToJsonElement(text).jsonObject["need_snapshot"]
                        ?.jsonPrimitive?.booleanOrNull
                }.getOrNull() ?: false
                if (needSnapshot) registered = false
            }
        } catch (e: RegistryHttpException) {
            if (e.status == 404 || e.status == 409) registered = false
            throw e
        }
        state = "online"
        lastOkAt = nowIso()
        lastError = null
        errorCount = 0
        lastLoggedError = null
    }

    private suspend fun runLoop() {
        var backoff = 1_000L
        // 侧车在局域网内：最多按心跳周期退避，保证阵图重启后一个周期内恢复
        val maxBackoff = max(15_000L, min(60_000L, intervalMs))
        while (scope.isActive) {
            try {
                tick()
                backoff = 1_000L
                delay(intervalMs)
            } catch (e: CancellationException) {
                throw e
            } catch (e: RegistryHttpException) {
                if (e.status == 404 || e.status == 409) {
                    registered = false
                    backoff = 1_000L
                }
                fail(e.code)
                delay(backoff)
                backoff = min(backoff * 2, maxBackoff)
            } catch (e: Exception) {
                fail(e.javaClass.simpleName)
                delay(backoff)
                backoff = min(backoff * 2, maxBackoff)
            }
        }
    }

    private fun fail(code: String) {
        state = "offline"
        lastError = code
        errorCount += 1
        if (code != lastLoggedError || errorCount % 10 == 1) {
            val message = "阵图注册异常（$code），本地服务不受影响"
            Log.w(TAG, message)
            NodeLogBuffer.record("WARNING", "registry", message)
            lastLoggedError = code
        }
    }

    private fun endpoint(): String =
        config.registryPublicEndpoint?.takeIf { it.isNotBlank() }
            ?: "http://${lanIpv4()}:${config.port}"

    private fun lanIpv4(): String = runCatching {
        Collections.list(NetworkInterface.getNetworkInterfaces())
            .asSequence()
            .flatMap { Collections.list(it.inetAddresses).asSequence() }
            .filterIsInstance<Inet4Address>()
            .firstOrNull { !it.isLoopbackAddress && !it.isLinkLocalAddress }
            ?.hostAddress
    }.getOrNull() ?: "127.0.0.1"

    private fun fallbackHealth(): HealthResponse = HealthResponse(
        node = NodeMeta(
            id = config.nodeId,
            name = config.nodeName,
            role = Contract.ROLE,
            arch = Contract.ARCH,
            runtime = Contract.RUNTIME,
            runtimeVersion = null,
            contractVersion = Contract.VERSION,
        ),
        status = NodeStatus.LOADING,
        models = config.capabilitiesMap.keys.map { name ->
            ModelEntry(
                name = name,
                contextLen = config.contextLenFor(name),
                loaded = false,
                capabilities = config.capabilityFor(name),
            )
        },
        resources = Resources(
            cpuPct = null,
            memUsedMb = null,
            memTotalMb = null,
            diskFreeMb = null,
            queueLen = 0,
        ),
        limits = Limits(maxConcurrency = config.maxConcurrency, maxContextLen = null),
        updatedAt = nowIso(),
    )

    private suspend fun post(path: String, body: String, timeout: Int = timeoutMs): String =
        withContext(Dispatchers.IO) {
            val conn = (URL("$baseUrl/zhentu/v1/nodes$path").openConnection() as HttpURLConnection)
                .apply {
                    requestMethod = "POST"
                    connectTimeout = timeout
                    readTimeout = timeout
                    doOutput = true
                    setRequestProperty("content-type", "application/json")
                    setRequestProperty("authorization", "Bearer ${token.orEmpty()}")
                    setRequestProperty("X-Node-Id", config.nodeId)
                    setRequestProperty("X-Node-Role", Contract.ROLE)
                    setRequestProperty("Contract-Version", Contract.VERSION)
                }
            try {
                conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
                val code = conn.responseCode
                val text = (if (code in 200..299) conn.inputStream else conn.errorStream)
                    ?.bufferedReader(Charsets.UTF_8)
                    ?.use { it.readText() }
                    .orEmpty()
                if (code !in 200..299) {
                    val errorCode = runCatching {
                        json.parseToJsonElement(text).jsonObject["error"]
                            ?.jsonObject?.get("code")?.jsonPrimitive?.content
                    }.getOrNull() ?: "http_$code"
                    throw RegistryHttpException(code, errorCode)
                }
                text
            } finally {
                conn.disconnect()
            }
        }

    private fun nowIso(): String =
        OffsetDateTime.now().format(DateTimeFormatter.ISO_OFFSET_DATE_TIME)

    companion object {
        private const val TAG = "NodeRegistryReporter"
    }
}
