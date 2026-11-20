package com.biyou.sanxian.orchestration

import java.io.File
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 用共享 golden fixture 校验 Kotlin 侧编排契约解析。
 *
 * fixture 与 Python 侧 `zhentu/tests/test_orchestration_fixtures.py` 共用，
 * 保证两端对 RequestEnvelope / Plan / TaskEvent / ResultEnvelope 的理解一致。
 */
class OrchestrationFixtureTest {

    private val json = Json { ignoreUnknownKeys = true }

    private fun fixtureFile(): File {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val candidate = File(
                dir,
                "docs/fixtures/orchestration/envelopes.v1.json",
            )
            if (candidate.isFile) return candidate
            dir = dir.parentFile
        }
        error("未找到编排契约 fixture，当前目录 ${File(".").absolutePath}")
    }

    private fun fixture() = json.parseToJsonElement(fixtureFile().readText()).jsonObject

    @Test
    fun requestEnvelopeParses() {
        val request = json.decodeFromJsonElement(
            RequestEnvelope.serializer(),
            fixture().getValue("request_envelope"),
        )
        assertEquals("1.0", request.schemaVersion)
        assertEquals("cluster", request.mode)
        assertEquals("auto", request.requestedMode)
        assertEquals("normal", request.privacy)
        assertTrue(request.deadlineS in 5..600)
        assertTrue(request.input.messages.isNotEmpty())
        assertTrue(request.requirements.capabilities.contains("chat"))
    }

    @Test
    fun planParsesWithOrderedDependencies() {
        val plan = json.decodeFromJsonElement(
            Plan.serializer(),
            fixture().getValue("plan"),
        )
        val ids = plan.steps.map { it.id }
        assertEquals(ids.size, ids.toSet().size)
        assertTrue(plan.maxSteps >= plan.steps.size)
        assertTrue(plan.maxParallel >= 1)

        val seen = mutableSetOf<String>()
        plan.steps.forEach { step ->
            assertTrue(step.failurePolicy in setOf("fail_task", "partial"))
            assertTrue(step.input.messages.isNotEmpty())
            assertTrue(seen.containsAll(step.dependsOn))
            seen.add(step.id)
        }
        assertTrue(ids.containsAll(plan.aggregate.inputStepIds))
    }

    @Test
    fun taskEventParsesAgainstPlan() {
        val event = json.decodeFromJsonElement(
            TaskEvent.serializer(),
            fixture().getValue("task_event"),
        )
        val plan = json.decodeFromJsonElement(
            Plan.serializer(),
            fixture().getValue("plan"),
        )
        assertTrue(event.seq >= 1)
        assertTrue(event.type in TASK_EVENT_TYPES)
        val stepId = event.data.getValue("step_id").jsonPrimitive.content
        assertTrue(plan.steps.any { it.id == stepId })
    }

    @Test
    fun resultEnvelopeParses() {
        val result = json.decodeFromJsonElement(
            ResultEnvelope.serializer(),
            fixture().getValue("result_envelope"),
        )
        val event = json.decodeFromJsonElement(
            TaskEvent.serializer(),
            fixture().getValue("task_event"),
        )
        assertEquals("1.0", result.schemaVersion)
        assertTrue(result.status in setOf("completed", "partial", "failed", "canceled"))
        assertEquals(event.taskId, result.taskId)
        assertEquals("partial", result.status)
        assertEquals("deadline", result.terminalReason)
        result.error?.let { assertEquals("orchestration_error", it.type) }
    }

    private companion object {
        val TASK_EVENT_TYPES = setOf(
            "task/created",
            "task/planned",
            "step/started",
            "step/result",
            "step/failed",
            "step/canceled",
            "step/retrying",
            "aggregate/started",
            "aggregate/result",
            "task/completed",
            "task/failed",
            "task/canceled",
            "task/interrupted",
            "task/resumed",
        )
    }
}
