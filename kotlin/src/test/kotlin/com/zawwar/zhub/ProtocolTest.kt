package com.zawwar.zhub

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull

class ProtocolTest {
    @Test
    fun envelopeRoundTrip() {
        val original = Envelope(
            type = "test",
            payload = buildJsonObject { put("hello", JsonPrimitive("world")) },
        )
        val text = original.toJson()
        val parsed = Envelope.fromJson(text)
        assertEquals(original.type, parsed.type)
        assertEquals(original.request_id, parsed.request_id)
    }

    @Test
    fun manifestRoundTrip() {
        val original = Manifest(
            name = "zai",
            description = "father's autonomous AI",
            capabilities = listOf(
                Capability(name = "chat", description = "openai-compat chat"),
                Capability(name = "memory", description = "vector recall"),
            ),
            public = true,
            operator = "zawwar",
        )
        val text = json.encodeToString(Manifest.serializer(), original)
        val parsed = json.decodeFromString(Manifest.serializer(), text)
        assertEquals(original.name, parsed.name)
        assertEquals(2, parsed.capabilities.size)
        assertEquals("chat", parsed.capabilities[0].name)
        assertEquals(true, parsed.public)
    }

    @Test
    fun chatRequestEnvelopeShape() {
        val env = chatRequestEnvelope(
            messages = listOf(mapOf("role" to "user", "content" to "hi")),
            model = "zai-sonnet",
        )
        assertEquals("chat-request", env.type)
        assertEquals(JsonPrimitive("zai-sonnet"), env.payload["model"])
        assertNotNull(env.payload["messages"])
    }

    @Test
    fun invokeResultEnvelopeShape() {
        val ok = invokeResultEnvelope("req_xyz", ok = true, result = buildJsonObject { put("delivered", JsonPrimitive(true)) })
        assertEquals("invoke-result", ok.type)
        assertEquals("req_xyz", ok.request_id)
        assertEquals(JsonPrimitive(true), ok.payload["ok"])
    }
}
