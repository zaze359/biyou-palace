package com.biyou.sanxian.node

import com.biyou.sanxian.contract.HealthResponse
import com.biyou.sanxian.contract.Limits
import com.biyou.sanxian.contract.ModelEntry
import com.biyou.sanxian.contract.NodeMeta
import com.biyou.sanxian.contract.Resources
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class NodeRegistryReporterTest {

    private fun models() = listOf(
        ModelEntry(
            name = "Qwen3-0.6B-Q8_0.gguf",
            loaded = true,
            capabilities = listOf("chat"),
            contextLen = 4096,
        ),
        ModelEntry(
            name = "Qwen3-Embedding-0.6B-Q8_0.gguf",
            loaded = false,
            capabilities = listOf("embedding"),
            contextLen = 8192,
        ),
    )

    private fun health(models: List<ModelEntry> = models()) = HealthResponse(
        node = NodeMeta(
            id = "sanxian-01",
            name = "散仙·01",
            role = "sanxian",
            arch = "arm64",
            runtime = "llama.cpp",
            runtimeVersion = "b0000",
            contractVersion = "1.0",
        ),
        status = "ready",
        models = models,
        resources = Resources(
            cpuPct = 1.0,
            memUsedMb = 100,
            memTotalMb = 200,
            diskFreeMb = 300,
            queueLen = 0,
            batteryPct = 50.0,
            temperatureC = 40.0,
            thermalState = "nominal",
        ),
        limits = Limits(maxConcurrency = 2, maxContextLen = 4096),
        updatedAt = "2026-11-14T10:00:00+08:00",
    )

    private fun config() = NodeConfig(
        nodeId = "sanxian-01",
        registryUrl = "http://192.168.1.10:4020",
        registryHeartbeatIntervalS = 30,
    )

    @Test
    fun `models revision ignores order and loaded`() {
        val first = RegistryPayload.modelsRevision(models())
        val second = RegistryPayload.modelsRevision(models().reversed().map { it.copy(loaded = false) })
        assertEquals(first, second)
        assertTrue(first.startsWith("sha256:"))
    }

    @Test
    fun `models revision changes when capability changes`() {
        val changed = models().mapIndexed { index, model ->
            if (index == 0) model.copy(capabilities = listOf("chat", "vision")) else model
        }
        assertTrue(
            RegistryPayload.modelsRevision(models()) != RegistryPayload.modelsRevision(changed)
        )
    }

    @Test
    fun `models revision matches cross language vector`() {
        val vector = listOf(
            ModelEntry(name = "m1", loaded = false, capabilities = listOf("chat"), contextLen = 100),
            ModelEntry(
                name = "m2",
                loaded = true,
                capabilities = listOf("embedding", "chat"),
                contextLen = 200,
            ),
        )
        // 与 Python/Registry 相同的协议向量：m1|chat|100\nm2|chat,embedding|200
        assertEquals(
            "sha256:6355eece20667e7f7642f57b2bb871c41e9a74b21f46f7b24b30d442704ee594",
            RegistryPayload.modelsRevision(vector),
        )
    }

    @Test
    fun `register body carries contract fields`() {
        val body = RegistryPayload.registerBody(
            config = config(),
            health = health(),
            endpoint = "http://192.168.1.31:4011",
            bootId = "boot-12345678",
            sentAt = "2026-11-14T10:00:00+08:00",
        )
        assertEquals("http://192.168.1.31:4011", body["endpoint"]!!.jsonPrimitive.content)
        assertEquals("ready", body["status"]!!.jsonPrimitive.content)
        assertEquals(30, body["heartbeat_interval_s"]!!.jsonPrimitive.int)
        assertEquals("boot-12345678", body["boot_id"]!!.jsonPrimitive.content)
        assertEquals("sanxian-01", body["node"]!!.jsonObject["id"]!!.jsonPrimitive.content)
        val bodyModels = body["models"]!!.jsonArray
        assertEquals(2, bodyModels.size)
        assertEquals(
            "Qwen3-0.6B-Q8_0.gguf",
            bodyModels[0].jsonObject["name"]!!.jsonPrimitive.content,
        )
        assertEquals(2, body["limits"]!!.jsonObject["max_concurrency"]!!.jsonPrimitive.int)
    }

    @Test
    fun `heartbeat body carries revision and loaded models only`() {
        val revision = RegistryPayload.modelsRevision(models())
        val body = RegistryPayload.heartbeatBody(
            health = health(),
            revision = revision,
            bootId = "boot-12345678",
            sentAt = "2026-11-14T10:00:15+08:00",
        )
        assertEquals(revision, body["models_rev"]!!.jsonPrimitive.content)
        assertEquals(
            listOf("Qwen3-0.6B-Q8_0.gguf"),
            body["loaded_models"]!!.jsonArray.map { it.jsonPrimitive.content },
        )
        assertNull(body["models"])
    }
}
