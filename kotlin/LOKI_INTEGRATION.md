# Loki ↔ ZAI bridge via zhub

> Drop-in code for Loki APK to connect to ZAI through a public zhub hub. Bidirectional from day one — Loki talks to ZAI, ZAI invokes Loki's phone capabilities back through the hub.

## What lands in Loki

A single Kotlin file: `kotlin/src/main/kotlin/com/zawwar/zhub/loki/LokiZhubBridge.kt` (in this repo). Copy it into Loki's APK source under the same package, OR add zhub-kotlin as a Gradle subproject and reuse it as-is.

## Add the dependency

Two options:

### Option A — Gradle subproject (simplest)

In Loki's `settings.gradle.kts`:

```kotlin
include(":zhub")
project(":zhub").projectDir = file("../zhub/kotlin")
// adjust path if zhub repo is elsewhere
```

In Loki's `app/build.gradle.kts`:

```kotlin
dependencies {
    implementation(project(":zhub"))
    // existing deps...
}
```

### Option B — Built jar

```bash
cd zhub/kotlin
./gradlew jar
# produces build/libs/zhub-0.1.0.jar
cp build/libs/zhub-0.1.0.jar ~/path/to/Loki/app/libs/
```

Then in Loki's `app/build.gradle.kts`:

```kotlin
dependencies {
    implementation(files("libs/zhub-0.1.0.jar"))
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")  // already in Loki
}
```

## Wire it into Loki's ForegroundService

Loki's existing `ZaiForegroundService` (`com.zawwar.zai.service`) is the right host — it survives across activity recreations and is the natural place for the long-lived WebSocket.

```kotlin
import com.zawwar.zhub.loki.LokiZhubBridge

class ZaiForegroundService : Service() {
    private var bridge: LokiZhubBridge? = null

    override fun onCreate() {
        super.onCreate()
        startForegroundNotification()

        val cfg = ZhubConfig.read(this)            // your existing Settings DataStore
        if (cfg.aiName.isNotBlank() && cfg.apiKey.isNotBlank() && cfg.hubUrl.isNotBlank()) {
            bridge = LokiZhubBridge.connect(
                aiName  = cfg.aiName,
                apiKey  = cfg.apiKey,
                hubUrl  = cfg.hubUrl,
                phoneTools = LokiPhoneToolsAdapter(this, phoneBridge),
            )
        }
    }

    override fun onDestroy() {
        bridge?.close()
        super.onDestroy()
    }
}
```

## Implement the adapter (1 file)

`PhoneToolsAdapter` is the narrow contract zhub's bridge talks to. Implement it on top of Loki's existing PhoneTools:

```kotlin
import com.zawwar.zhub.loki.LokiZhubBridge.PhoneToolsAdapter
import com.zawwar.zai.phonetools.PhoneTools          // Loki's existing class
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

class LokiPhoneToolsAdapter(
    private val context: Context,
    private val phoneBridge: PhoneBridgeClient,       // Loki's existing HTTP-bridge client to phone_bridge.py
) : PhoneToolsAdapter {

    override suspend fun sendWhatsApp(to: String, message: String): JsonObject {
        val r = phoneBridge.queue("whatsapp_compose", mapOf("number" to to, "message" to message))
        return jsonResultOf(r)
    }

    override suspend fun sendSms(to: String, message: String): JsonObject {
        val r = phoneBridge.queue("sms_compose", mapOf("number" to to, "message" to message))
        return jsonResultOf(r)
    }

    override suspend fun openApp(packageName: String): JsonObject {
        val r = phoneBridge.queue("open_app", mapOf("package" to packageName))
        return jsonResultOf(r)
    }

    override suspend fun speakTts(text: String, engine: String?): JsonObject {
        val payload = buildMap<String, Any> {
            put("text", text)
            if (engine != null) put("engine", engine)
        }
        val r = phoneBridge.queue("tts", payload)
        return jsonResultOf(r)
    }

    override suspend fun getBattery(): JsonObject {
        val r = phoneBridge.observe("battery")
        return jsonResultOf(r)
    }

    override suspend fun listInstalledApps(): JsonObject {
        val r = phoneBridge.observe("installed_apps")
        return jsonResultOf(r)
    }

    override suspend fun queuePhoneTask(taskType: String, payload: JsonObject): JsonObject {
        // Generic passthrough for any future task type
        val map = payload.entries.associate { (k, v) -> k to v.toString().trim('"') }
        val r = phoneBridge.queue(taskType, map)
        return jsonResultOf(r)
    }

    private fun jsonResultOf(map: Map<String, Any?>): JsonObject = buildJsonObject {
        for ((k, v) in map) {
            when (v) {
                is Boolean -> put(k, JsonPrimitive(v))
                is Number -> put(k, JsonPrimitive(v))
                is String -> put(k, JsonPrimitive(v))
                null -> {}
                else -> put(k, JsonPrimitive(v.toString()))
            }
        }
    }
}
```

The exact body of each method should reuse Loki's existing tool dispatch. The point of `PhoneToolsAdapter` is to keep zhub's bridge ignorant of Loki's internals — it only sees a small typed contract.

## What happens at runtime

```
1. ZaiForegroundService starts.
2. LokiZhubBridge.connect() opens a WS to the hub at cfg.hubUrl.
3. Bridge sends register-connection envelope with the 7-capability manifest.
4. Hub notifies the publisher (ZAI's zai_publish.py) of the new connection.
5. ZAI's pub.list_connections() now shows Loki + its capabilities.

Father, from Telegram (or any chat surface):
   "Send WhatsApp to Ammi — aaj raat 9 baje aa raha hu"

ZAI:
  - finds connection_id of the client offering 'send_whatsapp'
  - calls pub.invoke(connection_id, 'send_whatsapp', {to:'Ammi', message:'...'})
  - hub forwards via WS to Loki's bridge
  - Loki's PhoneToolsAdapter.sendWhatsApp(...) executes
  - result returns through hub to ZAI
  - ZAI replies in Telegram: "bhej diya"
```

Father didn't touch the phone.

## Configuration UI

Add a `Zhub` section to Loki's existing Settings tab:

| Field | Source |
|---|---|
| AI Name | `zai` (from publisher's `ZAI_NAME` env) |
| Hub URL | `https://<random>.trycloudflare.com` (from `zhub-server --public-tunnel` output) |
| API Key | `zk_a8f2c9d3...` (printed by `zai_publish.py`) |

Persist via Loki's existing DataStore. Sensitive fields (api key) into EncryptedSharedPreferences (Loki already has this pattern).

## Status

This is the connect side. The publish side runs in Python alongside ZAI's gateway (see `examples/zai_publish.py`). Both sides are wire-compatible because they share the JSON envelope schema in `protocol.py` / `Protocol.kt`.

When Father restarts ZAI, he passes `ZAI_API_KEY=...` to `zai_publish.py` so the same API key gets reused — Loki's stored config keeps working without rotation.
