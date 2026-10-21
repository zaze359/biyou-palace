package com.biyou.sanxian.llm

import kotlin.random.Random

enum class Role { User, Assistant, System }

/**
 * 单条对话消息。id 自动生成，作为 Compose LazyColumn 的稳定 key。
 */
data class ChatMessage(
    val id: String = Random.nextLong().toString(),
    val role: Role,
    val content: String,
) {
    companion object {
        fun user(text: String) = ChatMessage(role = Role.User, content = text)
        fun assistant(text: String) = ChatMessage(role = Role.Assistant, content = text)
    }
}
