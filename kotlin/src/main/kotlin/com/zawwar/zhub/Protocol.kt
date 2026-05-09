package com.zawwar.zhub

import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.JsonPrimitive
import java.util.UUID

/**
 * Wire envelope. Matches the Python implementation byte-for-byte.
 *
 * type:        message kind discriminator
 * request_id:  correlates request/response pairs
 * payload:     type-specific contents
 */
@Serializable
data class Envelope(
    val type: String,
    val request_id: String = newRequestId(),
    val payload: JsonObject = JsonObject(emptyMap()),
) {
    fun toJson(): String = json.encodeToString(this)

    companion object {
        fun fromJson(text: String): Envelope = json.decodeFromString(text)
    }
}

internal val json = Json {
    ignoreUnknownKeys = true
    encodeDefaults = false
}

internal fun newRequestId(): String = UUID.randomUUID().toString().replace("-", "")

// ---- helper constructors ------------------------------------------------

internal fun registerConnectionEnvelope(
    aiName: String,
    apiKey: String,
    clientManifest: Manifest,
): Envelope = Envelope(
    type = "register-connection",
    payload = buildJsonObject {
        put("ai_name", JsonPrimitive(aiName))
        put("api_key", JsonPrimitive(apiKey))
        put("client_manifest", json.encodeToJsonElement(Manifest.serializer(), clientManifest) as JsonObject)
    },
)

internal fun chatRequestEnvelope(
    messages: List<Map<String, String>>,
    model: String = "default",
    temperature: Double = 0.4,
    maxTokens: Int = 4096,
): Envelope {
    // Build the messages array explicitly — generic List<Map<...>> doesn't
    // resolve cleanly through Json.encodeToJsonElement on Kotlin 2.0+, and an
    // explicit construction avoids the reified-type ambiguity entirely.
    val messagesArray = kotlinx.serialization.json.JsonArray(
        messages.map { m ->
            buildJsonObject {
                for ((k, v) in m) put(k, JsonPrimitive(v))
            }
        }
    )
    return Envelope(
        type = "chat-request",
        payload = buildJsonObject {
            put("messages", messagesArray)
            put("model", JsonPrimitive(model))
            put("temperature", JsonPrimitive(temperature))
            put("max_tokens", JsonPrimitive(maxTokens))
        },
    )
}

internal fun invokeResultEnvelope(
    requestId: String,
    ok: Boolean,
    result: JsonElement? = null,
    error: String? = null,
): Envelope = Envelope(
    type = "invoke-result",
    request_id = requestId,
    payload = buildJsonObject {
        put("ok", JsonPrimitive(ok))
        if (result != null) put("result", result)
        if (error != null) put("error", JsonPrimitive(error))
    },
)
