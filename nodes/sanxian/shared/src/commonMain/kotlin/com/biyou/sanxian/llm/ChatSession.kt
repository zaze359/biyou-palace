package com.biyou.sanxian.llm

/**
 * 一次对话会话，包含历史消息与可选的系统提示词。
 * 传给 [LlmEngine.generate] 用于构造上下文。
 */
data class ChatSession(
    val messages: List<ChatMessage> = emptyList(),
    val systemPrompt: String? = null,
)
