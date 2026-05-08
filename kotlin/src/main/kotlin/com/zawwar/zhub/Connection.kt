package com.zawwar.zhub

import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.*
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

/**
 * Capability handler. Receives a JSON-shaped args object, returns a JSON-shaped result.
 * Suspending so the implementation can await native APIs (e.g., Termux IPC, intent
 * dispatch on Android, network calls, etc).
 */
typealias CapabilityHandler = suspend (JsonObject) -> JsonObject

/**
 * Bidirectional zhub connection from a client (Loki, Telegram bot, web chat, ...)
 * to a published AI. Connects to `<hubUrl>/ws/connect`, registers the client's
 * capability manifest, then:
 *   - Allows the client to call `chat(...)` on the AI through the hub.
 *   - Listens for invoke-request envelopes and dispatches them to registered handlers.
 *
 * Lifecycle:
 *   val conn = ZhubConnection.connect(...)
 *   conn.events.collect { ... }              // optional: observe lifecycle
 *   val response = conn.chat(messages)       // talk to the AI
 *   conn.close()                             // graceful shutdown
 */
class ZhubConnection private constructor(
    val aiName: String,
    val apiKey: String,
    val hubUrl: String,
    val clientManifest: Manifest,
    private val capabilities: Map<String, Pair<JsonObject, CapabilityHandler>>,
    private val scope: CoroutineScope,
) {
    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private var webSocket: WebSocket? = null
    private val pending = ConcurrentHashMap<String, CompletableDeferred<JsonObject>>()
    private val _events = MutableSharedFlow<ConnectionEvent>(replay = 0, extraBufferCapacity = 64)
    val events: SharedFlow<ConnectionEvent> = _events.asSharedFlow()

    private fun start() {
        val wsUrl = toWsUrl(hubUrl, "/ws/connect")
        val request = Request.Builder().url(wsUrl).build()
        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(ws: WebSocket, response: Response) {
                ws.send(registerConnectionEnvelope(aiName, apiKey, clientManifest).toJson())
                scope.launch { _events.emit(ConnectionEvent.Opened) }
            }

            override fun onMessage(ws: WebSocket, text: String) {
                scope.launch { handleMessage(ws, text) }
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                scope.launch { _events.emit(ConnectionEvent.Failed(t)) }
            }

            override fun onClosing(ws: WebSocket, code: Int, reason: String) {
                ws.close(code, reason)
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                scope.launch { _events.emit(ConnectionEvent.Closed(code, reason)) }
            }
        })
    }

    private suspend fun handleMessage(ws: WebSocket, text: String) {
        val env = try {
            Envelope.fromJson(text)
        } catch (t: Throwable) {
            _events.emit(ConnectionEvent.MalformedPayload(text, t))
            return
        }
        when (env.type) {
            "registered" -> _events.emit(ConnectionEvent.Registered)
            "chat-response" -> {
                pending.remove(env.request_id)?.complete(env.payload)
            }
            "invoke-request" -> {
                val capability = env.payload["capability"]?.jsonPrimitive?.content ?: return
                val args = env.payload["args"] as? JsonObject ?: JsonObject(emptyMap())
                val handler = capabilities[capability]?.second
                if (handler == null) {
                    ws.send(invokeResultEnvelope(env.request_id, false, error = "capability '$capability' not exposed").toJson())
                    return
                }
                try {
                    val result = handler(args)
                    ws.send(invokeResultEnvelope(env.request_id, true, result = result).toJson())
                } catch (t: Throwable) {
                    ws.send(invokeResultEnvelope(env.request_id, false, error = t.message ?: t.javaClass.simpleName).toJson())
                }
            }
            "error" -> {
                _events.emit(ConnectionEvent.HubError(env.payload.toString()))
                val code = env.payload["code"]?.jsonPrimitive?.content
                if (code == "register_failed") {
                    pending.values.forEach { it.completeExceptionally(AuthException(env.payload.toString())) }
                    pending.clear()
                }
            }
            "ping" -> {
                ws.send(Envelope(type = "pong", request_id = env.request_id).toJson())
            }
        }
    }

    /** Send a chat request to the AI through the hub. Suspending. Times out at [timeoutMs]. */
    suspend fun chat(
        messages: List<Map<String, String>>,
        model: String = "default",
        temperature: Double = 0.4,
        maxTokens: Int = 4096,
        timeoutMs: Long = 60_000,
    ): String = withTimeout(timeoutMs) {
        val ws = webSocket ?: throw ZhubConnectionException("not connected")
        val env = chatRequestEnvelope(messages, model, temperature, maxTokens)
        val deferred = CompletableDeferred<JsonObject>()
        pending[env.request_id] = deferred
        try {
            ws.send(env.toJson())
            val payload = deferred.await()
            payload["text"]?.jsonPrimitive?.content ?: ""
        } finally {
            pending.remove(env.request_id)
        }
    }

    fun close() {
        webSocket?.close(1000, "client closing")
        client.dispatcher.executorService.shutdown()
    }

    sealed class ConnectionEvent {
        object Opened : ConnectionEvent()
        object Registered : ConnectionEvent()
        data class Closed(val code: Int, val reason: String) : ConnectionEvent()
        data class Failed(val cause: Throwable) : ConnectionEvent()
        data class HubError(val payload: String) : ConnectionEvent()
        data class MalformedPayload(val raw: String, val cause: Throwable) : ConnectionEvent()
    }

    companion object {
        /**
         * Connect a client to a published AI and expose capabilities back to it.
         *
         * @param aiName       The AI's registered name on the hub.
         * @param apiKey       The bearer key returned to the publisher at register time.
         * @param hubUrl       Hub URL (http://, https://, ws://, or wss://).
         * @param description  Human-readable description of this client.
         * @param operator     Who runs this client.
         * @param capabilities Map of capabilityName -> (jsonSchemaForArgs, handler).
         * @param scope        Coroutine scope. Defaults to Dispatchers.Default + a SupervisorJob.
         */
        fun connect(
            aiName: String,
            apiKey: String,
            hubUrl: String = "ws://localhost:8080",
            description: String = "",
            operator: String = "",
            capabilities: Map<String, Pair<JsonObject, CapabilityHandler>>,
            scope: CoroutineScope = CoroutineScope(Dispatchers.Default + SupervisorJob()),
        ): ZhubConnection {
            val capList = capabilities.map { (name, pair) ->
                Capability(name = name, description = "", schema = pair.first)
            }
            val manifest = Manifest(
                name = "$aiName-client",
                description = description.ifEmpty { "client of $aiName" },
                operator = operator,
                capabilities = capList,
            )
            val conn = ZhubConnection(
                aiName = aiName,
                apiKey = apiKey,
                hubUrl = hubUrl,
                clientManifest = manifest,
                capabilities = capabilities,
                scope = scope,
            )
            conn.start()
            return conn
        }

        private fun toWsUrl(input: String, path: String): String {
            val replaced = when {
                input.startsWith("https://") -> "wss://" + input.removePrefix("https://")
                input.startsWith("http://") -> "ws://" + input.removePrefix("http://")
                else -> input
            }
            return if (replaced.endsWith("/")) replaced + path.removePrefix("/") else replaced + path
        }
    }
}

/** Convenience top-level for parity with Python's `from zhub import connect`. */
fun connect(
    aiName: String,
    apiKey: String,
    hubUrl: String = "ws://localhost:8080",
    description: String = "",
    operator: String = "",
    capabilities: Map<String, Pair<JsonObject, CapabilityHandler>>,
    scope: CoroutineScope = CoroutineScope(Dispatchers.Default + SupervisorJob()),
): ZhubConnection = ZhubConnection.connect(aiName, apiKey, hubUrl, description, operator, capabilities, scope)
