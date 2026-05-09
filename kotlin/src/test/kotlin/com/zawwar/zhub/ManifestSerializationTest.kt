package com.zawwar.zhub

import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class ManifestSerializationTest {
    /** Wire format must match what zhub Python emits. If a Python field is
     *  added without updating Kotlin, this test catches it on next CI run. */
    @Test
    fun decodesPythonProducedManifestJson() {
        // Sample matching Python's Manifest(...).to_dict() then json.dumps
        val pythonJson = """
            {
              "schema_version": "0.1",
              "name": "zai",
              "description": "Father's autonomous AI",
              "accepts": "openai-v1-chat-completions",
              "auth": {"type": "bearer"},
              "rate_limit": "60/min",
              "capabilities": [
                {"name": "chat", "description": "openai-compat", "schema": {"type": "object"}, "auth_tier": "default", "notes": ""}
              ],
              "public": true,
              "operator": "zawwar",
              "contact": "",
              "extensions": {}
            }
        """.trimIndent()
        val parsed = Json { ignoreUnknownKeys = true }.decodeFromString(Manifest.serializer(), pythonJson)
        assertEquals("zai", parsed.name)
        assertEquals(true, parsed.public)
        assertEquals(1, parsed.capabilities.size)
        assertEquals("chat", parsed.capabilities[0].name)
    }

    @Test
    fun encodesKotlinManifestForPython() {
        val m = Manifest(
            name = "test",
            description = "round trip",
            capabilities = listOf(Capability(name = "chat", description = "x")),
            public = true,
        )
        val text = Json { encodeDefaults = true }.encodeToString(Manifest.serializer(), m)
        // Python expects these field names exactly
        assertTrue("\"name\":\"test\"" in text || "\"name\": \"test\"" in text,
                   "expected name field, got: $text")
        assertTrue("\"public\":true" in text || "\"public\": true" in text,
                   "expected public:true, got: $text")
    }
}
