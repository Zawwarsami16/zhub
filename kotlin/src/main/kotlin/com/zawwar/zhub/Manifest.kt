package com.zawwar.zhub

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

/**
 * Manifest format. Same wire-shape as the Python implementation. v0.1.
 */
@Serializable
data class Manifest(
    val schema_version: String = "0.1",
    val name: String = "",
    val description: String = "",
    val accepts: String = "openai-v1-chat-completions",
    val auth: Map<String, String> = mapOf("type" to "bearer"),
    val rate_limit: String = "60/min",
    val capabilities: List<Capability> = emptyList(),
    val public: Boolean = false,
    val operator: String = "",
    val contact: String = "",
    val extensions: Map<String, JsonElement> = emptyMap(),
)

@Serializable
data class Capability(
    val name: String,
    val description: String,
    val schema: JsonObject = JsonObject(emptyMap()),
    val returns: JsonObject? = null,
    val auth_tier: String = "default",
    val rate_limit: String? = null,
    val notes: String = "",
)

/** Convenience builder — for clients that only expose capabilities back to the AI. */
fun capabilityManifest(
    name: String,
    description: String = "",
    operator: String = "",
    capabilities: List<Capability> = emptyList(),
): Manifest = Manifest(
    name = name,
    description = description,
    operator = operator,
    capabilities = capabilities,
)
