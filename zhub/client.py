"""
Client-side library — both publish() and connect() modes.

Both modes use the same WebSocket transport against the hub. Differ only in
which endpoint they connect to and which envelopes they send/receive.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlparse

try:
    import websockets
except ImportError as e:
    raise SystemExit(
        "zhub client needs websockets. install with:\n"
        "    pip install 'websockets>=12'"
    ) from e

from .manifest import Capability, Manifest, chat_only_manifest
from .protocol import (
    Envelope, register_publisher, register_connection, chat_request, chat_chunk,
    invoke_request, invoke_result,
)
from .errors import AuthError, ConnectionError as ZhubConnectionError


log = logging.getLogger("zhub.client")


# Type aliases for handlers
ChatHandler = Callable[[list[dict[str, Any]], dict[str, Any]], Any]
"""(messages, options) -> str OR coroutine OR dict-with-{text, finish_reason, usage}"""

CapabilityHandler = Callable[[dict[str, Any]], Any]
"""(args) -> result OR coroutine"""

ConnectionEventHandler = Callable[[str, str, Optional[dict[str, Any]]], None]
"""(kind, connection_id, client_manifest) -> None"""


def _to_ws_url(http_or_ws_url: str, ws_path: str) -> str:
    """Convert https://hub.example.com → wss://hub.example.com/{ws_path}.
    Accepts already-ws URLs. Preserves port + path prefix."""
    u = urlparse(http_or_ws_url)
    scheme = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}.get(u.scheme, "wss")
    netloc = u.netloc or u.path  # in case user passed bare host
    if not netloc:
        raise ValueError(f"could not parse hub url: {http_or_ws_url}")
    return f"{scheme}://{netloc}{ws_path}"


# ---- publish mode --------------------------------------------------------

@dataclass
class ZhubPublication:
    """Returned by publish(). The handle to a running publisher."""
    name: str
    base_url: str
    api_key: str
    manifest: Manifest
    hub_url: str
    chat_handler: ChatHandler
    on_connection_event: Optional[ConnectionEventHandler] = None
    _task: Optional[asyncio.Task] = None
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    _connections: dict[str, dict[str, Any]] = field(default_factory=dict)

    def list_connections(self) -> list[dict[str, Any]]:
        """Snapshot of currently connected clients + their capabilities."""
        return [
            {"connection_id": cid, **info}
            for cid, info in self._connections.items()
        ]

    def find_capability(self, capability_name: str) -> Optional[str]:
        """Return the connection_id of the first client offering a given capability, or None."""
        for cid, info in self._connections.items():
            for cap in info.get("client_manifest", {}).get("capabilities", []):
                if cap.get("name") == capability_name:
                    return cid
        return None

    async def invoke(self, connection_id: str, capability: str,
                     args: Optional[dict[str, Any]] = None,
                     timeout: float = 60.0) -> Any:
        """Call back into a connected client's capability through the hub."""
        if not getattr(self, "_ws", None):
            raise ZhubConnectionError("publisher not connected to hub")
        env = invoke_request(connection_id, capability, args or {})
        future = asyncio.get_running_loop().create_future()
        self._pending[env.request_id] = future
        try:
            await self._ws.send(env.to_json())
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(env.request_id, None)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()


def publish(
    name: str,
    description: str,
    chat_handler: ChatHandler,
    hub_url: str = "ws://localhost:8080",
    capabilities: Optional[list[Capability]] = None,
    public: bool = False,
    operator: str = "",
    contact: str = "",
    on_connection_event: Optional[ConnectionEventHandler] = None,
    api_key: Optional[str] = None,
    private_key: Optional[str] = None,
) -> ZhubPublication:
    """Create a ZhubPublication. Call .run_forever() to actually start serving.

    If `api_key` is supplied AND the hub has a stored publisher with the
    same name and matching key hash, this is a re-registration after a hub
    restart — the same name + key are reused. Otherwise a fresh registration
    is performed and a new key is allocated.

    If `private_key` (hex-encoded ed25519 private key) is supplied, the
    manifest is signed before publish. The hub validates the signature on
    register and stores the public key. Consumers fetching
    `/<name>/manifest.json` can verify identity without trusting the hub.
    """
    manifest = chat_only_manifest(
        name=name, description=description,
        operator=operator, contact=contact, public=public,
    )
    if capabilities:
        manifest.capabilities.extend(capabilities)
    pub = ZhubPublication(
        name=name,
        base_url="",
        api_key="",
        manifest=manifest,
        hub_url=hub_url,
        chat_handler=chat_handler,
        on_connection_event=on_connection_event,
    )
    pub._pending: dict[str, asyncio.Future] = {}  # type: ignore[attr-defined]
    pub._ws = None  # type: ignore[attr-defined]

    async def runner() -> None:
        url = _to_ws_url(hub_url, "/ws/publish")
        log.info("publisher connecting to %s", url)
        async with websockets.connect(url, max_size=10_000_000) as ws:
            pub._ws = ws  # type: ignore[attr-defined]
            manifest_dict = manifest.to_dict()
            if private_key:
                from .signing import sign_manifest as _sign
                manifest_dict = _sign(manifest_dict, private_key)
            register_env = register_publisher(manifest_dict, name)
            if api_key:
                register_env.payload["api_key"] = api_key
            await ws.send(register_env.to_json())

            async for raw in ws:
                env = Envelope.from_json(raw)

                if env.type == "registered":
                    pub.name = env.payload.get("name", name)
                    pub.base_url = env.payload.get("base_url", "")
                    pub.api_key = env.payload.get("api_key", "")
                    log.info("publisher registered as %s with key %s",
                             pub.name, pub.api_key[:10] + "…")

                elif env.type == "chat-request":
                    asyncio.create_task(_handle_chat(pub, ws, env))

                elif env.type == "invoke-result":
                    fut = pub._pending.get(env.request_id)  # type: ignore[attr-defined]
                    if fut and not fut.done():
                        fut.set_result(env.payload)

                elif env.type == "connection-event":
                    kind = env.payload.get("kind", "?")
                    cid = env.payload.get("connection_id", "")
                    cm = env.payload.get("client_manifest")
                    if kind == "connected":
                        pub._connections[cid] = {"client_manifest": cm}
                    elif kind == "disconnected":
                        pub._connections.pop(cid, None)
                    elif kind == "updated":
                        pub._connections[cid] = {"client_manifest": cm}
                    if on_connection_event:
                        on_connection_event(kind, cid, cm)

                elif env.type == "error":
                    log.warning("hub error: %s", env.payload)

    pub._task = asyncio.ensure_future(runner())
    return pub


async def _handle_chat(pub: ZhubPublication, ws, env: Envelope) -> None:
    import inspect

    messages = env.payload.get("messages", [])
    options = {k: v for k, v in env.payload.items() if k != "messages"}
    streaming_requested = bool(options.get("stream"))
    try:
        result = pub.chat_handler(messages, options)

        # Streaming first — before iscoroutine check, since some Python versions
        # treat sync generators ambiguously and `await` on a generator throws.
        # If the caller requested streaming, emit chat-chunk per yield. Otherwise
        # accumulate the generator output into a single chat-response so non-
        # streaming HTTP callers don't time out.
        if inspect.isasyncgen(result):
            if streaming_requested:
                async for chunk in result:
                    await ws.send(chat_chunk(str(chunk), env.request_id).to_json())
                await ws.send(chat_chunk("", env.request_id, done=True, finish_reason="stop").to_json())
                return
            else:
                accumulated = []
                async for chunk in result:
                    accumulated.append(str(chunk))
                payload = {"text": "".join(accumulated), "finish_reason": "stop"}
                await ws.send(Envelope(type="chat-response", request_id=env.request_id, payload=payload).to_json())
                return
        if inspect.isgenerator(result):
            if streaming_requested:
                for chunk in result:
                    await ws.send(chat_chunk(str(chunk), env.request_id).to_json())
                await ws.send(chat_chunk("", env.request_id, done=True, finish_reason="stop").to_json())
                return
            else:
                accumulated = "".join(str(c) for c in result)
                payload = {"text": accumulated, "finish_reason": "stop"}
                await ws.send(Envelope(type="chat-response", request_id=env.request_id, payload=payload).to_json())
                return

        # Coroutine — await for the final value
        if inspect.iscoroutine(result):
            result = await result

        # Single-shot
        if isinstance(result, str):
            payload = {"text": result, "finish_reason": "stop"}
        elif isinstance(result, dict):
            payload = result
            payload.setdefault("finish_reason", "stop")
            payload.setdefault("text", "")
        else:
            payload = {"text": str(result), "finish_reason": "stop"}
        await ws.send(Envelope(
            type="chat-response", request_id=env.request_id, payload=payload
        ).to_json())
    except Exception as e:
        log.exception("chat handler raised")
        await ws.send(Envelope(
            type="chat-response", request_id=env.request_id,
            payload={"text": f"[chat handler error] {e}", "finish_reason": "error"}
        ).to_json())


# ---- connect mode --------------------------------------------------------

@dataclass
class ZhubConnection:
    """Returned by connect(). The handle to a running client."""
    ai_name: str
    api_key: str
    hub_url: str
    client_manifest: Manifest
    capabilities: dict[str, CapabilityHandler]
    _task: Optional[asyncio.Task] = None
    _ws: Any = None
    _pending: dict[str, asyncio.Future] = field(default_factory=dict)
    _streams: dict[str, asyncio.Queue] = field(default_factory=dict)

    async def chat(self, messages: list[dict[str, Any]],
                   model: str = "default", temperature: float = 0.4,
                   max_tokens: int = 4096, timeout: float = 60.0) -> dict[str, Any]:
        """Send a chat request through the hub to the AI."""
        env = chat_request(messages, model, temperature, max_tokens)
        future = asyncio.get_running_loop().create_future()
        self._pending[env.request_id] = future
        try:
            await self._ws.send(env.to_json())
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(env.request_id, None)

    async def chat_stream(self, messages: list[dict[str, Any]],
                          model: str = "default", temperature: float = 0.4,
                          max_tokens: int = 4096, timeout_per_chunk: float = 60.0):
        """Async iterator over streaming chunks from the AI.

        Usage:
            async for chunk in conn.chat_stream(messages=[...]):
                print(chunk, end="", flush=True)
        """
        env = chat_request(messages, model, temperature, max_tokens,
                           extras={"stream": True})
        queue: asyncio.Queue = asyncio.Queue()
        self._streams[env.request_id] = queue
        try:
            await self._ws.send(env.to_json())
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=timeout_per_chunk)
                except asyncio.TimeoutError:
                    break
                if item is None:
                    break
                if item.get("done"):
                    break
                yield item.get("delta", "")
        finally:
            self._streams.pop(env.request_id, None)


def connect(
    ai_name: str,
    api_key: str,
    capabilities: dict[str, tuple[dict[str, Any], CapabilityHandler]],
    hub_url: str = "ws://localhost:8080",
    description: str = "",
    operator: str = "",
) -> ZhubConnection:
    """Connect a client to a published AI. Capabilities is a dict of
    {capability_name: (json_schema_for_args, handler_function)}."""
    cm = Manifest(
        name=f"{ai_name}-client",
        description=description or f"client of {ai_name}",
        operator=operator,
        capabilities=[
            Capability(name=cap_name, description="", schema=schema)
            for cap_name, (schema, _) in capabilities.items()
        ],
    )
    handlers = {name: handler for name, (_, handler) in capabilities.items()}
    conn = ZhubConnection(
        ai_name=ai_name,
        api_key=api_key,
        hub_url=hub_url,
        client_manifest=cm,
        capabilities=handlers,
    )

    async def runner() -> None:
        url = _to_ws_url(hub_url, "/ws/connect")
        log.info("client connecting to %s for AI %s", url, ai_name)
        async with websockets.connect(url, max_size=10_000_000) as ws:
            conn._ws = ws
            await ws.send(register_connection(ai_name, api_key, cm.to_dict()).to_json())

            async for raw in ws:
                env = Envelope.from_json(raw)

                if env.type == "registered":
                    log.info("client registered to %s", ai_name)

                elif env.type == "chat-response":
                    fut = conn._pending.get(env.request_id)
                    if fut and not fut.done():
                        fut.set_result(env.payload)
                    queue = conn._streams.get(env.request_id)
                    if queue is not None:
                        await queue.put({"delta": env.payload.get("text", ""), "done": False})
                        await queue.put({"done": True})

                elif env.type == "chat-chunk":
                    queue = conn._streams.get(env.request_id)
                    if queue is not None:
                        await queue.put(env.payload)

                elif env.type == "invoke-request":
                    asyncio.create_task(_handle_invoke(conn, ws, env))

                elif env.type == "error":
                    log.warning("hub error: %s", env.payload)
                    if env.payload.get("code") == "register_failed":
                        raise AuthError(env.payload.get("message", "register failed"))

    conn._task = asyncio.ensure_future(runner())
    return conn


async def _handle_invoke(conn: ZhubConnection, ws, env: Envelope) -> None:
    capability = env.payload.get("capability", "")
    args = env.payload.get("args", {})
    handler = conn.capabilities.get(capability)
    if handler is None:
        await ws.send(invoke_result(env.request_id, ok=False,
                                    error=f"capability '{capability}' not exposed").to_json())
        return
    try:
        result = handler(args)
        if asyncio.iscoroutine(result):
            result = await result
        await ws.send(invoke_result(env.request_id, ok=True, result=result).to_json())
    except Exception as e:
        log.exception("capability handler raised")
        await ws.send(invoke_result(env.request_id, ok=False, error=str(e)).to_json())
