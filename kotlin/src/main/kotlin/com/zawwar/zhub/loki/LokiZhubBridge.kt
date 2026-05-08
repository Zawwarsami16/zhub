package com.zawwar.zhub.loki

import com.zawwar.zhub.CapabilityHandler
import com.zawwar.zhub.ZhubConnection
import com.zawwar.zhub.connect
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/**
 * Drop-in bridge between Loki and a published AI (typically ZAI) over zhub.
 *
 * Designed to be invoked from a long-lived ForegroundService inside Loki's
 * existing APK. Once connected:
 *
 *   - Loki can call `bridge.chat(...)` to talk to the AI.
 *   - The AI can invoke any of Loki's exposed phone capabilities through
 *     the hub (send WhatsApp, send SMS, TTS, open app, get battery, etc).
 *
 * Usage from Loki's Service:
 *
 *     val bridge = LokiZhubBridge.connect(
 *         aiName = settings.aiName,
 *         apiKey = settings.apiKey,
 *         hubUrl = settings.hubUrl,
 *         phoneTools = phoneTools,           // existing Loki PhoneTools instance
 *     )
 *
 *     // talk to the AI from Loki:
 *     val reply = bridge.connection.chat(
 *         messages = listOf(mapOf("role" to "user", "content" to "kya hua?")),
 *     )
 *
 *     // teardown:
 *     bridge.close()
 *
 * The AI can now invoke any of Loki's PhoneTools capabilities through zhub
 * — every invocation goes through Loki's existing tool layer, so the same
 * permissions, audit, and firewall rules already enforced in Loki apply
 * uniformly.
 */
class LokiZhubBridge private constructor(
    val connection: ZhubConnection,
) {
    fun close() {
        connection.close()
    }

    /**
     * Adapter interface — Loki implements this with its own PhoneTools.
     * Kept narrow so we can mock it in tests and so the bridge doesn't pull
     * in Android-specific imports.
     */
    interface PhoneToolsAdapter {
        suspend fun sendWhatsApp(to: String, message: String): JsonObject
        suspend fun sendSms(to: String, message: String): JsonObject
        suspend fun openApp(packageName: String): JsonObject
        suspend fun speakTts(text: String, engine: String? = null): JsonObject
        suspend fun getBattery(): JsonObject
        suspend fun listInstalledApps(): JsonObject
        suspend fun queuePhoneTask(taskType: String, payload: JsonObject): JsonObject
    }

    companion object {
        /**
         * Build the standard set of Loki capabilities + connect.
         * The capability set mirrors Loki's own PhoneTools surface so the AI
         * sees the same vocabulary regardless of which transport it uses.
         */
        fun connect(
            aiName: String,
            apiKey: String,
            hubUrl: String,
            phoneTools: PhoneToolsAdapter,
            description: String = "Loki — Father's phone bridge",
            operator: String = "zawwar",
            scope: CoroutineScope = CoroutineScope(Dispatchers.Default + SupervisorJob()),
        ): LokiZhubBridge {
            val capabilities: Map<String, Pair<JsonObject, CapabilityHandler>> = buildMap {
                put("send_whatsapp", whatsappSchema to { args ->
                    phoneTools.sendWhatsApp(
                        to = args["to"]?.let { it.toString().trim('"') } ?: "",
                        message = args["message"]?.let { it.toString().trim('"') } ?: "",
                    )
                })
                put("send_sms", smsSchema to { args ->
                    phoneTools.sendSms(
                        to = args["to"]?.let { it.toString().trim('"') } ?: "",
                        message = args["message"]?.let { it.toString().trim('"') } ?: "",
                    )
                })
                put("open_app", openAppSchema to { args ->
                    phoneTools.openApp(args["package"]?.let { it.toString().trim('"') } ?: "")
                })
                put("speak_tts", ttsSchema to { args ->
                    phoneTools.speakTts(
                        text = args["text"]?.let { it.toString().trim('"') } ?: "",
                        engine = args["engine"]?.let { it.toString().trim('"') },
                    )
                })
                put("get_battery", emptySchema to { _ -> phoneTools.getBattery() })
                put("list_installed_apps", emptySchema to { _ -> phoneTools.listInstalledApps() })
                put("queue_phone_task", queueTaskSchema to { args ->
                    phoneTools.queuePhoneTask(
                        taskType = args["task_type"]?.let { it.toString().trim('"') } ?: "",
                        payload = (args["payload"] as? JsonObject) ?: JsonObject(emptyMap()),
                    )
                })
            }
            val connection = connect(
                aiName = aiName,
                apiKey = apiKey,
                hubUrl = hubUrl,
                description = description,
                operator = operator,
                capabilities = capabilities,
                scope = scope,
            )
            return LokiZhubBridge(connection)
        }

        // Schemas — kept terse, matching Loki's actual tool signatures.

        private val emptySchema = buildJsonObject {
            put("type", JsonPrimitive("object"))
            put("properties", JsonObject(emptyMap()))
        }

        private val whatsappSchema = buildJsonObject {
            put("type", JsonPrimitive("object"))
            put("required", kotlinx.serialization.json.JsonArray(listOf(JsonPrimitive("to"), JsonPrimitive("message"))))
            put("properties", buildJsonObject {
                put("to", buildJsonObject { put("type", JsonPrimitive("string")) })
                put("message", buildJsonObject { put("type", JsonPrimitive("string")) })
            })
        }

        private val smsSchema = whatsappSchema

        private val openAppSchema = buildJsonObject {
            put("type", JsonPrimitive("object"))
            put("required", kotlinx.serialization.json.JsonArray(listOf(JsonPrimitive("package"))))
            put("properties", buildJsonObject {
                put("package", buildJsonObject { put("type", JsonPrimitive("string")) })
            })
        }

        private val ttsSchema = buildJsonObject {
            put("type", JsonPrimitive("object"))
            put("required", kotlinx.serialization.json.JsonArray(listOf(JsonPrimitive("text"))))
            put("properties", buildJsonObject {
                put("text", buildJsonObject { put("type", JsonPrimitive("string")) })
                put("engine", buildJsonObject { put("type", JsonPrimitive("string")) })
            })
        }

        private val queueTaskSchema = buildJsonObject {
            put("type", JsonPrimitive("object"))
            put("required", kotlinx.serialization.json.JsonArray(listOf(JsonPrimitive("task_type"))))
            put("properties", buildJsonObject {
                put("task_type", buildJsonObject { put("type", JsonPrimitive("string")) })
                put("payload", buildJsonObject { put("type", JsonPrimitive("object")) })
            })
        }
    }
}
