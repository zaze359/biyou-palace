package com.biyou.sanxian.orchestration

import com.biyou.sanxian.contract.ChatMessage
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

/**
 * 阵图编排协议 v1.0 的 Kotlin 数据模型。
 *
 * 与 [docs/orchestration-contract.md] 逐字段对齐，供节点前端 / LocalOrchestrator
 * 构造请求与解析结果；共享 golden fixture 见
 * `docs/fixtures/orchestration/envelopes.v1.json`。
 *
 * 这里只定义协议模型，不包含编排运行时。
 */

@Serializable
data class RequestEnvelope(
    @SerialName("schema_version") val schemaVersion: String,
    @SerialName("request_id") val requestId: String,
    @SerialName("session_id") val sessionId: String? = null,
    @SerialName("origin_node_id") val originNodeId: String,
    val mode: String,
    @SerialName("requested_mode") val requestedMode: String? = null,
    val privacy: String,
    @SerialName("deadline_s") val deadlineS: Int,
    val input: RequestInput,
    val requirements: RequestRequirements,
)

@Serializable
data class RequestInput(
    val messages: List<ChatMessage>,
    val attachments: List<JsonElement> = emptyList(),
)

@Serializable
data class RequestRequirements(
    val capabilities: List<String>,
    @SerialName("context_len") val contextLen: Int? = null,
)

@Serializable
data class Plan(
    @SerialName("plan_id") val planId: String,
    val intent: String? = null,
    @SerialName("max_steps") val maxSteps: Int,
    @SerialName("max_parallel") val maxParallel: Int,
    val steps: List<PlanStep>,
    val aggregate: AggregateSpec,
)

@Serializable
data class PlanStep(
    val id: String,
    val goal: String,
    val capability: String,
    val input: StepInput,
    @SerialName("depends_on") val dependsOn: List<String> = emptyList(),
    @SerialName("parallel_group") val parallelGroup: String? = null,
    val exclusive: Boolean = false,
    val required: Boolean = true,
    @SerialName("failure_policy") val failurePolicy: String = "fail_task",
    @SerialName("model_hint") val modelHint: String? = null,
    @SerialName("input_ref") val inputRef: String? = null,
    @SerialName("output_schema") val outputSchema: JsonElement? = null,
)

@Serializable
data class StepInput(
    val messages: List<ChatMessage>,
)

@Serializable
data class AggregateSpec(
    val mode: String,
    @SerialName("input_step_ids") val inputStepIds: List<String>,
    @SerialName("include_partial") val includePartial: Boolean = false,
    val instructions: String? = null,
)

@Serializable
data class TaskEvent(
    @SerialName("event_id") val eventId: String,
    @SerialName("task_id") val taskId: String,
    val seq: Int,
    val type: String,
    val at: String,
    val data: JsonObject = JsonObject(emptyMap()),
)

@Serializable
data class ResultEnvelope(
    @SerialName("schema_version") val schemaVersion: String,
    @SerialName("task_id") val taskId: String,
    val status: String,
    @SerialName("terminal_reason") val terminalReason: String? = null,
    val mode: String,
    val answer: String? = null,
    @SerialName("answer_ref") val answerRef: String? = null,
    val sources: List<ResultSource> = emptyList(),
    val partials: List<PartialResult> = emptyList(),
    val usage: ResultUsage,
    val error: OrchestrationError? = null,
)

@Serializable
data class ResultSource(
    @SerialName("step_id") val stepId: String,
    @SerialName("node_id") val nodeId: String,
    val model: String,
    @SerialName("route_decision_id") val routeDecisionId: String? = null,
    @SerialName("output_ref") val outputRef: String? = null,
)

@Serializable
data class PartialResult(
    @SerialName("step_id") val stepId: String,
    val reason: String,
    @SerialName("output_ref") val outputRef: String? = null,
)

@Serializable
data class ResultUsage(
    @SerialName("prompt_tokens") val promptTokens: Int = 0,
    @SerialName("completion_tokens") val completionTokens: Int = 0,
    val steps: Int = 0,
)

@Serializable
data class OrchestrationError(
    val code: String,
    val message: String,
    val type: String = "orchestration_error",
    val param: String? = null,
)
