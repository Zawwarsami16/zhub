# zhub-kotlin

Kotlin/JVM client library for [zhub](https://github.com/Zawwarsami16/zhub). Drop-in for Android (Loki) and any JVM project that needs to **connect** to a published AI and expose capabilities back to it bidirectionally.

This is the connect-side mirror of the Python `zhub.connect()` API. Same wire protocol, same envelope schema, same hub server.

## Add to your project

In your Android / JVM module's `build.gradle.kts`:

```kotlin
dependencies {
    // Until published to Maven Central, add as a Gradle subproject.
    // From the zhub repo: kotlin/build.gradle.kts
    implementation(project(":zhub"))

    // Required transitive deps (already in Loki):
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
```

## Usage

```kotlin
import com.zawwar.zhub.connect
import com.zawwar.zhub.CapabilityHandler
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.coroutines.runBlocking

val sendWhatsApp: CapabilityHandler = { args ->
    // Your real implementation — talk to phone bridge, etc.
    buildJsonObject {
        put("delivered", JsonPrimitive(true))
        put("to", args["to"] ?: JsonPrimitive("?"))
    }
}

val whatsappSchema = buildJsonObject {
    put("type", JsonPrimitive("object"))
    put("required", kotlinx.serialization.json.JsonArray(listOf(JsonPrimitive("to"), JsonPrimitive("message"))))
    put("properties", buildJsonObject {
        put("to", buildJsonObject { put("type", JsonPrimitive("string")) })
        put("message", buildJsonObject { put("type", JsonPrimitive("string")) })
    })
}

fun main() = runBlocking {
    val conn = connect(
        aiName = "zai",
        apiKey = "zk_a8f2c9d3...",
        hubUrl = "https://hub.example.com",
        description = "Loki — Father's phone bridge",
        operator = "zawwar",
        capabilities = mapOf(
            "send_whatsapp" to (whatsappSchema to sendWhatsApp),
        ),
    )

    // Talk to the AI from this client.
    val reply = conn.chat(messages = listOf(
        mapOf("role" to "user", "content" to "kya chal raha hai?"),
    ))
    println("ZAI: $reply")

    // The AI can now invoke `send_whatsapp` through the hub at any time.
    // We just keep the connection alive — the WebSocket handles dispatch.
}
```

## Loki integration sketch

Inside Loki's existing Compose app, register the connection in your `ForegroundService` (so the WebSocket survives across activity recreations):

```kotlin
class LokiZhubBridgeService : Service() {
    private var conn: ZhubConnection? = null

    override fun onCreate() {
        super.onCreate()
        val cfg = readZaiEndpointConfig(this)  // your existing settings store
        conn = connect(
            aiName = cfg.aiName,
            apiKey = cfg.apiKey,
            hubUrl = cfg.hubUrl,
            capabilities = mapOf(
                "send_whatsapp" to (whatsappSchema to ::handleWhatsApp),
                "send_sms"      to (smsSchema      to ::handleSms),
                "open_app"      to (openAppSchema  to ::handleOpenApp),
                "speak_tts"     to (ttsSchema      to ::handleTts),
                "get_battery"   to (batterySchema  to ::handleBattery),
            ),
        )
    }

    override fun onDestroy() {
        conn?.close()
        super.onDestroy()
    }
}
```

The handlers reuse Loki's existing tool-dispatch — just adapt them to take `JsonObject` args and return `JsonObject` results.

## Build + test

```bash
cd kotlin
./gradlew test
./gradlew jar
```

The output jar at `kotlin/build/libs/zhub-0.1.0.jar` can be dropped into any JVM project as a flat dependency.

## Status

Phase 0.3 — published with the Python core. Wire-protocol-compatible. Tests for envelope + manifest serialization. Real WebSocket lifecycle. No publish() side yet (an AI typically runs Python; the Kotlin lib is connect-only by design — but the symmetry is one weekend of work if you want a Kotlin AI to publish itself).
