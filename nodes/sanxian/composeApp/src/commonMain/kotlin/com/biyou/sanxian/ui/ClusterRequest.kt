package com.biyou.sanxian.ui

import com.biyou.sanxian.contract.ChatContent
import com.biyou.sanxian.contract.ChatMessage as ContractChatMessage
import com.biyou.sanxian.llm.ChatMessage
import com.biyou.sanxian.orchestration.RequestEnvelope
import com.biyou.sanxian.orchestration.RequestInput
import com.biyou.sanxian.orchestration.RequestRequirements
import kotlin.random.Random

/**
 * 把对话历史裁剪成阵图 `RequestEnvelope`。
 *
 * 纯函数，不含网络与状态；`local_only` 在这里直接拒绝，避免把本机数据发到阵图。
 */
object ClusterRequestEnvelopeBuilder {

    const val MAX_HISTORY = 20
    const val MAX_CONTENT_CHARS = 8000
    const val MIN_DEADLINE_S = 5
    const val MAX_DEADLINE_S = 600

    private val CANONICAL_CAPABILITIES = setOf("chat", "vision", "ocr", "embedding")

    fun build(
        originNodeId: String,
        requestedMode: ChatMode,
        messages: List<ChatMessage>,
        sessionId: String? = null,
        privacy: String = "normal",
        deadlineSeconds: Int = 120,
        capabilities: List<String> = listOf("chat"),
    ): RequestEnvelope {
        require(originNodeId.isNotBlank()) { "origin_node_id 不能为空" }
        require(requestedMode != ChatMode.LOCAL) { "本地模式不提交阵图" }
        require(privacy != "local_only") { "local_only 任务不允许提交到阵图" }

        val bounded = messages
            .filter { it.content.isNotBlank() }
            .takeLast(MAX_HISTORY)
            .map {
                ContractChatMessage(
                    role = it.role.name.lowercase(),
                    content = ChatContent.Text(it.content.take(MAX_CONTENT_CHARS)),
                )
            }
        require(bounded.isNotEmpty()) { "没有可提交的消息" }

        val canonicalCaps = capabilities
            .map { it.trim().lowercase() }
            .filter { it in CANONICAL_CAPABILITIES }
            .distinct()
            .ifEmpty { listOf("chat") }

        return RequestEnvelope(
            schemaVersion = "1.0",
            requestId = "req-${randomSuffix()}",
            sessionId = sessionId,
            originNodeId = originNodeId,
            // 阵图只接受 cluster；用户选择 auto 时通过 requested_mode 审计。
            mode = "cluster",
            requestedMode = requestedMode.wire,
            privacy = privacy,
            deadlineS = deadlineSeconds.coerceIn(MIN_DEADLINE_S, MAX_DEADLINE_S),
            input = RequestInput(messages = bounded),
            requirements = RequestRequirements(capabilities = canonicalCaps),
        )
    }

    private fun randomSuffix(): String =
        Random.nextLong(1, Long.MAX_VALUE).toString(36) + Random.nextInt(1, Int.MAX_VALUE).toString(36)
}
