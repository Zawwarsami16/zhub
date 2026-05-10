"""
zhub hub server.

Runs anywhere with Python 3.10+. Maintains:
  - A registry of publishers (the AIs).
  - A registry of connections (clients connected to each AI).
  - A WebSocket multiplex per publisher, per connection.
  - Public HTTP endpoints for OpenAI-compatible chat completions and manifest
    discovery.

The server is intentionally tiny — most of the protocol logic lives in
`protocol.py`. The hub is just a router.

Run:
    python -m zhub.server --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
import os
import asyncio
import json  # noqa: F401  -- used in inline SSE serialization
import logging
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
    import uvicorn
except ImportError as e:
    raise SystemExit(
        "zhub.server requires fastapi + uvicorn. install with:\n"
        "    pip install 'fastapi>=0.110' 'uvicorn[standard]>=0.27'"
    ) from e

from .persistence import Storage, hash_key
from .ratelimit import parse_rate, SlidingWindow
from .validate import validate as validate_schema
from .protocol import (
    Envelope, registered, chat_request, chat_response,
    invoke_request, invoke_result, connection_event, error_envelope, new_request_id,
)


log = logging.getLogger("zhub.server")
access_log = logging.getLogger("zhub.access")
# Make sure access logs appear by default — uvicorn's default config
# leaves the root logger at WARNING for app loggers.
if not access_log.handlers:
    access_log.setLevel(logging.INFO)


# ---- registry -----------------------------------------------------------

@dataclass
class PublisherRegistration:
    """A live AI publisher."""
    name: str
    manifest: dict[str, Any]
    websocket: WebSocket
    api_key_hash: str  # hashed copy of the key; raw key handed to publisher only
    created_at: float = field(default_factory=time.time)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)


@dataclass
class ConnectionRegistration:
    """A client connected to a particular publisher."""
    connection_id: str
    ai_name: str
    websocket: WebSocket
    client_manifest: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)


class Hub:
    def __init__(self, storage: Optional[Storage] = None) -> None:
        self.publishers: dict[str, PublisherRegistration] = {}
        self.connections_by_ai: dict[str, dict[str, ConnectionRegistration]] = defaultdict(dict)
        self.api_keys: dict[str, str] = {}  # api_key -> ai_name (for fast lookup)
        self.lock = asyncio.Lock()
        self.storage = storage
        # request_id -> (client_websocket, ai_name) for streaming relay from
        # ws_connect-side chat-requests. Publisher emits chat-chunk back; we
        # route by request_id to the originating client.
        self.client_routes: dict[str, tuple[WebSocket, str]] = {}
        # Rate-limit windows per AI. Each AI gets its own SlidingWindow
        # configured from the publisher's manifest.rate_limit. Key into the
        # window is the api_key string used by the caller.
        self._rate_windows: dict[str, SlidingWindow] = {}
        # Hub identity for cross-hub loop prevention. Stable per process; can
        # be pinned via ZHUB_HUB_ID for deterministic forwarding chains.
        self.hub_id = os.environ.get("ZHUB_HUB_ID") or "hub_" + secrets.token_urlsafe(6)
        # Observability counters. Per-AI dict-of-dicts so /metrics can show
        # which publisher is hot. Counters are best-effort, not transactional.
        self.started_at = time.time()
        self.metrics: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def bump(self, ai_name: str, key: str, delta: int = 1) -> None:
        self.metrics[ai_name][key] += delta

    def record_latency(self, ai_name: str, latency_ms: float) -> None:
        """Track per-AI request latency. Stores total_ms + count for avg
        and max for the obvious upper bound. Called by the access-log
        middleware after every request whose path identifies an AI."""
        bucket = self.metrics[ai_name]
        bucket["request_count"] += 1
        bucket["total_latency_ms"] += int(latency_ms)
        if int(latency_ms) > bucket.get("max_latency_ms", 0):
            bucket["max_latency_ms"] = int(latency_ms)

    def check_rate_limit(self, ai_name: str, api_key: str) -> tuple[bool, Optional[float]]:
        """Returns (allowed, retry_after_seconds_or_None)."""
        window = self._rate_windows.get(ai_name)
        if window is None:
            publisher = self.publishers.get(ai_name)
            if publisher is None:
                return True, None  # AI offline; chat will 404 later
            limit, period = parse_rate(publisher.manifest.get("rate_limit"))
            window = SlidingWindow(limit=limit, period_seconds=period)
            self._rate_windows[ai_name] = window
        return window.check(api_key)

    # publishers --------------------------------------------------------

    async def register_publisher(self, name: str, manifest: dict[str, Any],
                                 websocket: WebSocket,
                                 desired_api_key: Optional[str] = None) -> tuple[str, str]:
        """Register an AI. Returns (assigned_name, api_key).

        If `desired_api_key` is supplied AND it matches a previously-stored
        publisher with the same name, this is a re-registration after a hub
        restart — keep the same name + same api_key. Otherwise allocate a
        fresh name + api_key.

        Signed manifests: if `manifest` carries a `signature` + `public_key`,
        the signature is verified before accepting the registration. On
        re-registration with `desired_api_key`, the supplied public_key must
        also match the stored manifest's public_key (key pinning — prevents
        takeover via stolen api_key alone). Unsigned manifests still accepted
        for backwards compatibility with v0 clients.
        """
        # Signature verification (if manifest is signed)
        if manifest.get("signature"):
            try:
                from .signing import verify_manifest as _verify
            except SystemExit:
                raise PermissionError(
                    "manifest carries a signature but hub lacks 'cryptography'; "
                    "install zhub with [crypto] extras"
                )
            if not _verify(manifest):
                raise PermissionError("manifest signature verification failed")

        async with self.lock:
            # Re-registration path (after hub restart / publisher restart)
            if desired_api_key and self.storage:
                stored = self.storage.lookup_publisher(name)
                if stored and stored["api_key_hash"] == hash_key(desired_api_key):
                    # Key pinning: if the stored manifest was signed, the
                    # incoming manifest must present the same public_key
                    # (otherwise a stolen api_key alone would let an attacker
                    # take over the registration).
                    stored_pk = stored["manifest"].get("public_key")
                    if stored_pk:
                        if manifest.get("public_key") != stored_pk:
                            raise PermissionError(
                                "key pinning: stored public_key does not match"
                            )
                    api_key_hash = stored["api_key_hash"]
                    self.publishers[name] = PublisherRegistration(
                        name=name,
                        manifest=manifest,
                        websocket=websocket,
                        api_key_hash=api_key_hash,
                    )
                    self.api_keys[desired_api_key] = name
                    self.storage.upsert_publisher(name, manifest, api_key_hash)
                    log.info("publisher re-registered: %s (existing key)", name)
                    return name, desired_api_key

            # Fresh registration
            assigned = name
            i = 1
            while assigned in self.publishers or (self.storage and self.storage.lookup_publisher(assigned)):
                i += 1
                assigned = f"{name}-{i}"
            api_key = "zk_" + secrets.token_urlsafe(24)
            api_key_hash = hash_key(api_key)
            self.publishers[assigned] = PublisherRegistration(
                name=assigned,
                manifest=manifest,
                websocket=websocket,
                api_key_hash=api_key_hash,
            )
            self.api_keys[api_key] = assigned
            if self.storage:
                self.storage.upsert_publisher(assigned, manifest, api_key_hash)
            log.info("publisher registered: %s", assigned)
            return assigned, api_key

    async def unregister_publisher(self, name: str) -> None:
        async with self.lock:
            self.publishers.pop(name, None)
            for k, v in list(self.api_keys.items()):
                if v == name:
                    self.api_keys.pop(k, None)
            self.connections_by_ai.pop(name, None)
            log.info("publisher unregistered: %s", name)

    def lookup_by_api_key(self, api_key: str) -> Optional[str]:
        return self.api_keys.get(api_key)

    def find_capability_connection(self, ai_name: str, capability_name: str) -> Optional[str]:
        """First connection_id under ai_name whose client_manifest exposes
        a capability matching capability_name, or None."""
        for cid, conn in self.connections_by_ai.get(ai_name, {}).items():
            for cap in conn.client_manifest.get("capabilities", []):
                if cap.get("name") == capability_name:
                    return cid
        return None

    def find_capability_schema(self, ai_name: str, capability_name: str) -> Optional[dict[str, Any]]:
        """Return the JSON-schema declared for a connected capability, or None."""
        for conn in self.connections_by_ai.get(ai_name, {}).values():
            for cap in conn.client_manifest.get("capabilities", []):
                if cap.get("name") == capability_name:
                    schema = cap.get("schema")
                    return schema if isinstance(schema, dict) else None
        return None

    # connections -------------------------------------------------------

    async def register_connection(self, ai_name: str, api_key: str,
                                  client_manifest: dict[str, Any],
                                  websocket: WebSocket) -> str:
        if ai_name not in self.publishers:
            raise ValueError("unknown AI")
        if self.api_keys.get(api_key) != ai_name:
            raise PermissionError("invalid api key for this AI")
        connection_id = "cx_" + secrets.token_urlsafe(8)
        reg = ConnectionRegistration(
            connection_id=connection_id,
            ai_name=ai_name,
            websocket=websocket,
            client_manifest=client_manifest,
        )
        async with self.lock:
            self.connections_by_ai[ai_name][connection_id] = reg
        # Notify publisher about the new connection
        await self._send_to_publisher(
            ai_name,
            connection_event("connected", connection_id, client_manifest),
        )
        log.info("connection registered: %s -> %s", connection_id, ai_name)
        return connection_id

    async def unregister_connection(self, ai_name: str, connection_id: str) -> None:
        async with self.lock:
            self.connections_by_ai.get(ai_name, {}).pop(connection_id, None)
        if ai_name in self.publishers:
            await self._send_to_publisher(
                ai_name,
                connection_event("disconnected", connection_id, None),
            )
        log.info("connection unregistered: %s", connection_id)

    # routing -----------------------------------------------------------

    def build_tools_for(self, ai_name: str,
                         caller_tools: Optional[list[dict[str, Any]]] = None,
                         ) -> list[dict[str, Any]]:
        """Compose an OpenAI-style `tools` array for an AI's chat-request.

        Each connected client's capabilities are turned into one
        function-tool entry. Caller-supplied tools win on name collisions
        (caller is closest to the user's intent)."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tool in caller_tools or []:
            name = (tool.get("function") or {}).get("name", "") if isinstance(tool, dict) else ""
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(tool)
        for conn in self.connections_by_ai.get(ai_name, {}).values():
            for cap in conn.client_manifest.get("capabilities", []) or []:
                cap_name = cap.get("name", "")
                if not cap_name or cap_name in seen:
                    continue
                seen.add(cap_name)
                out.append({
                    "type": "function",
                    "function": {
                        "name": cap_name,
                        "description": cap.get("description", "") or "",
                        "parameters": cap.get("schema") or {"type": "object"},
                    },
                })
        return out

    async def proxy_chat(self, ai_name: str, messages: list[dict[str, Any]],
                         model: str, temperature: float, max_tokens: int,
                         timeout: float = 60.0,
                         stream: bool = False,
                         tools: Optional[list[dict[str, Any]]] = None,
                         tool_choice: Optional[Any] = None) -> dict[str, Any]:
        """Route an HTTP chat request to the publisher and await its response.
        If stream=True the future delivers a queue of streaming chunks instead."""
        publisher = self.publishers.get(ai_name)
        if publisher is None:
            raise LookupError("publisher not registered")
        extras: dict[str, Any] = {}
        if stream:
            extras["stream"] = True
        if tools:
            extras["tools"] = tools
        if tool_choice is not None:
            extras["tool_choice"] = tool_choice
        env = chat_request(messages=messages, model=model,
                           temperature=temperature, max_tokens=max_tokens,
                           extras=extras or None)
        if stream:
            queue: asyncio.Queue = asyncio.Queue()
            publisher.pending[env.request_id] = queue  # type: ignore[assignment]
            await publisher.websocket.send_text(env.to_json())
            return {"_stream_queue": queue, "_request_id": env.request_id}
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        publisher.pending[env.request_id] = future
        try:
            await publisher.websocket.send_text(env.to_json())
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            publisher.pending.pop(env.request_id, None)

    async def invoke_capability(self, ai_name: str, connection_id: str,
                                capability: str, args: dict[str, Any],
                                timeout: float = 60.0) -> dict[str, Any]:
        """Publisher asked the hub to invoke a capability on a connection."""
        conn = self.connections_by_ai.get(ai_name, {}).get(connection_id)
        if conn is None:
            raise LookupError("connection not found")
        env = invoke_request(connection_id, capability, args)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        conn.pending[env.request_id] = future
        try:
            await conn.websocket.send_text(env.to_json())
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            conn.pending.pop(env.request_id, None)

    async def _send_to_publisher(self, ai_name: str, env: Envelope) -> None:
        publisher = self.publishers.get(ai_name)
        if publisher is None:
            return
        try:
            await publisher.websocket.send_text(env.to_json())
        except Exception as e:
            log.warning("failed to send to publisher %s: %s", ai_name, e)




# ---- FastAPI app --------------------------------------------------------

def create_app(db_path: Optional[str] = None) -> FastAPI:
    storage = Storage(db_path) if db_path else None
    hub = Hub(storage=storage)
    app = FastAPI(title="zhub", version="0.1.0")

    # Open CORS so browser-only OpenAI-compatible clients (Pocket, custom
    # web UIs, etc.) can hit the chat endpoint without a same-origin proxy.
    # Auth still happens via Bearer key — CORS only governs which origin
    # the browser will let the response back to.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Zhub-Origin", "X-Zhub-Forwarded-By", "Retry-After"],
    )

    def _model_entry(name: str, manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": name,
            "object": "model",
            "created": int(time.time()),
            "owned_by": manifest.get("operator") or "zhub",
        }

    @app.get("/v1/models")
    async def hub_models() -> JSONResponse:
        """OpenAI-compat model list. Many BYOK clients (Pocket, openai-py,
        etc.) call this on save to verify the provider. We list every
        currently-registered publisher as one model, so a single base URL
        of `<hub>/v1` works as a multi-AI provider."""
        data = [
            _model_entry(name, p.manifest)
            for name, p in hub.publishers.items()
        ]
        return JSONResponse({"object": "list", "data": data})

    @app.get("/{ai_name}/v1/models")
    async def ai_models(ai_name: str) -> JSONResponse:
        """Per-AI model list (used when the client's base URL is
        `<hub>/<ai>/v1`). Returns a single entry — that AI."""
        publisher = hub.publishers.get(ai_name)
        if publisher is None:
            raise HTTPException(404, f"AI '{ai_name}' not registered")
        return JSONResponse({
            "object": "list",
            "data": [_model_entry(ai_name, publisher.manifest)],
        })

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "publishers": str(len(hub.publishers))}

    # --- entity (zhub's self-knowledge layer) -----------------------------
    # Single markdown file shipped with the hub. Any AI attached can fetch
    # it and become instantly fluent in zhub — routes, errors, patterns,
    # debug recipes, perf tips. Sectioned access for cheap lookups.

    import functools as _functools
    import datetime as _datetime
    from pathlib import Path as _Path

    # --- caps for operator-added entity extensions (Phase 4.1) ----------
    _MAX_EXTENSION_BODY_BYTES = 8 * 1024
    _MAX_EXTENSIONS = 200

    @_functools.lru_cache(maxsize=1)
    def _shipped_entity_text() -> str:
        path = _Path(__file__).parent / "entity.md"
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "# zhub entity\n(entity.md missing in this install)\n"

    def _shipped_section(name: str) -> Optional[str]:
        text = _shipped_entity_text()
        target = f"## {name.lower().strip()}"
        out: list[str] = []
        capturing = False
        for line in text.splitlines():
            stripped = line.strip().lower()
            if stripped.startswith("## "):
                if capturing:
                    break
                if stripped == target:
                    capturing = True
                    out.append(line)
                    continue
            if capturing:
                out.append(line)
        return "\n".join(out).rstrip() + "\n" if out else None

    def _shipped_error(code: str) -> Optional[str]:
        section = _shipped_section("errors")
        if section is None:
            return None
        target = code.lower().strip()
        out: list[str] = []
        capturing = False
        for line in section.splitlines():
            stripped = line.strip()
            if stripped.startswith("### "):
                if capturing:
                    break
                heading = stripped[4:].lower().replace("`", " ").replace("'", " ")
                tokens = heading.split()
                if target in tokens:
                    capturing = True
                    out.append(line)
                    continue
            if capturing:
                out.append(line)
        return "\n".join(out).rstrip() + "\n" if out else None

    def _format_extension(ext: dict) -> str:
        date = _datetime.datetime.fromtimestamp(ext["added_at"]).date().isoformat()
        return (
            f"### `{ext['title']}` *(user-added by {ext['added_by']} on {date})*\n"
            f"{ext['body']}\n"
        )

    def _list_extensions(section: Optional[str] = None) -> list[dict[str, Any]]:
        if hub.storage is None:
            return []
        return hub.storage.list_entity_extensions(section=section)

    def _ext_matches_code(ext: dict, code: str) -> bool:
        target = code.lower().strip()
        title = ext["title"].lower().replace("`", " ").replace("'", " ")
        tokens = title.split()
        return target in tokens or title.strip() == target

    def _entity_text() -> str:
        base = _shipped_entity_text()
        exts = _list_extensions()
        if not exts:
            return base
        by_section: dict[str, list[dict]] = {}
        for e in exts:
            by_section.setdefault(e["section"], []).append(e)
        appendix_lines = ["## extensions",
                          "",
                          "> Operator-added recipes for this specific hub deployment. "
                          "Shipped recipes above always win on conflicts.",
                          ""]
        for section_name, items in by_section.items():
            appendix_lines.append(f"### applied to: {section_name}")
            appendix_lines.append("")
            for e in items:
                appendix_lines.append(_format_extension(e))
        return base.rstrip() + "\n\n---\n\n" + "\n".join(appendix_lines).rstrip() + "\n"

    def _entity_section(name: str) -> Optional[str]:
        base = _shipped_section(name)
        exts = _list_extensions(section=name)
        if base is None and not exts:
            return None
        chunks: list[str] = []
        if base:
            chunks.append(base.rstrip())
        for e in exts:
            chunks.append(_format_extension(e).rstrip())
        return "\n\n".join(chunks).rstrip() + "\n"

    def _entity_error(code: str) -> Optional[str]:
        base = _shipped_error(code)
        exts = _list_extensions(section="errors")
        matching = [e for e in exts if _ext_matches_code(e, code)]
        if base is None and not matching:
            return None
        parts: list[str] = []
        if base:
            parts.append(base.rstrip())
        for e in matching:
            parts.append(_format_extension(e).rstrip())
        return "\n\n".join(parts).rstrip() + "\n"

    @app.middleware("http")
    async def _access_log_and_latency(request, call_next):
        """Per-request access log + per-AI latency tracking.
        Logs at INFO via the `zhub.access` logger:
            <status> <method> <path> <latency_ms>ms [ai=<name>]
        For paths matching /<ai>/v1/* the AI name is also tracked in
        the hub's per-AI metrics bucket."""
        start = time.perf_counter()
        response = await call_next(request)
        latency_ms = (time.perf_counter() - start) * 1000.0
        path = request.url.path
        ai_name: Optional[str] = None
        # Heuristic: routes that start with /<ai>/v1/... or /<ai>/manifest.json
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] in hub.publishers:
            ai_name = parts[0]
        elif len(parts) >= 1 and parts[0] in hub.publishers:
            ai_name = parts[0]
        if ai_name is not None:
            hub.record_latency(ai_name, latency_ms)
        access_log.info(
            "%d %s %s %.0fms%s",
            response.status_code, request.method, path, latency_ms,
            f" ai={ai_name}" if ai_name else "",
        )
        return response

    @app.middleware("http")
    async def _add_entity_hint_header(request, call_next):
        """On any 4xx/5xx response whose status code has a recipe in
        entity.md, set X-Zhub-Entity-Hint pointing at /entity/errors/<code>.
        Purely additive — body shape unchanged. Lets any AI calling the
        hub self-debug by following the hint to the relevant section."""
        response = await call_next(request)
        if 400 <= response.status_code < 600:
            code = str(response.status_code)
            if _entity_error(code) is not None:
                response.headers["X-Zhub-Entity-Hint"] = f"/entity/errors/{code}"
        return response

    @app.get("/entity")
    async def entity_full() -> PlainTextResponse:
        return PlainTextResponse(_entity_text(), media_type="text/markdown")

    # --- entity extensions (Phase 4.1) ----------------------------------
    # Operator-added recipes appended to the shipped entity. Auth: any
    # registered publisher's bearer key. Persisted in SQLite so they
    # survive hub restarts.

    @app.post("/entity/extend")
    async def entity_extend(request: Request) -> JSONResponse:
        if hub.storage is None:
            raise HTTPException(503, "entity extensions require a hub --db; "
                                     "this hub started without persistence")
        api_key = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        added_by = hub.lookup_by_api_key(api_key)
        if not added_by:
            raise HTTPException(401, "invalid api key — extension requires a "
                                     "registered publisher's bearer key")
        body = await request.json()
        section = (body.get("section") or "").strip().lower()
        title = (body.get("title") or "").strip()
        ext_body = body.get("body") or ""
        if not section or not title or not ext_body:
            raise HTTPException(400, "missing required fields: section, title, body")
        if len(ext_body.encode("utf-8")) > _MAX_EXTENSION_BODY_BYTES:
            raise HTTPException(413,
                f"body too large (max {_MAX_EXTENSION_BODY_BYTES} bytes)")
        if hub.storage.count_entity_extensions() >= _MAX_EXTENSIONS:
            raise HTTPException(429,
                f"too many extensions (max {_MAX_EXTENSIONS}); delete some first")
        eid = hub.storage.add_entity_extension(
            section=section, title=title, body=ext_body, added_by=added_by,
        )
        return JSONResponse({
            "id": eid, "section": section, "title": title, "added_by": added_by,
        })

    @app.get("/entity/extend")
    async def entity_extend_list(request: Request) -> JSONResponse:
        if hub.storage is None:
            raise HTTPException(503, "entity extensions require a hub --db")
        api_key = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hub.lookup_by_api_key(api_key):
            raise HTTPException(401, "invalid api key")
        return JSONResponse({"extensions": hub.storage.list_entity_extensions()})

    @app.delete("/entity/extend/{ext_id}")
    async def entity_extend_delete(ext_id: int, request: Request) -> JSONResponse:
        if hub.storage is None:
            raise HTTPException(503, "entity extensions require a hub --db")
        api_key = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hub.lookup_by_api_key(api_key):
            raise HTTPException(401, "invalid api key")
        ok = hub.storage.delete_entity_extension(ext_id)
        if not ok:
            raise HTTPException(404, f"no extension with id {ext_id}")
        return JSONResponse({"deleted": True, "id": ext_id})

    @app.get("/entity/errors/{code}")
    async def entity_error(code: str) -> PlainTextResponse:
        body = _entity_error(code)
        if body is None:
            raise HTTPException(404, f"no entity entry for error '{code}'")
        return PlainTextResponse(body, media_type="text/markdown")

    @app.get("/entity/{section}")
    async def entity_section(section: str) -> PlainTextResponse:
        body = _entity_section(section)
        if body is None:
            raise HTTPException(404,
                f"no entity section '{section}'. try: routes, errors, patterns, debug, perf, conventions, architecture")
        return PlainTextResponse(body, media_type="text/markdown")

    @app.get("/metrics")
    async def metrics() -> JSONResponse:
        """Best-effort observability snapshot. Counters reset on hub restart."""
        by_ai: dict[str, dict[str, Any]] = {}
        for name, counts in hub.metrics.items():
            entry = dict(counts)
            count = entry.get("request_count", 0)
            total = entry.get("total_latency_ms", 0)
            entry["avg_latency_ms"] = (total // count) if count > 0 else 0
            by_ai[name] = entry
        for name, p in hub.publishers.items():
            entry = by_ai.setdefault(name, {})
            entry["connections"] = len(hub.connections_by_ai.get(name, {}))
            entry["uptime_seconds"] = int(time.time() - p.created_at)
        return JSONResponse({
            "hub_id": hub.hub_id,
            "uptime_seconds": int(time.time() - hub.started_at),
            "publishers": len(hub.publishers),
            "connections": sum(len(c) for c in hub.connections_by_ai.values()),
            "by_ai": by_ai,
        })

    @app.get("/")
    async def index_html() -> JSONResponse:
        # Returns the registry HTML page rendered from the template
        from fastapi.responses import HTMLResponse
        live = []
        for name, p in hub.publishers.items():
            if p.manifest.get("public"):
                live.append({
                    "name": name,
                    "description": p.manifest.get("description", ""),
                    "operator": p.manifest.get("operator", ""),
                    "capabilities": [c.get("name") for c in p.manifest.get("capabilities", [])],
                    "connections": len(hub.connections_by_ai.get(name, {})),
                    "uptime_seconds": int(time.time() - p.created_at),
                    "online": True,
                })
        # Also include known-but-offline publishers from storage
        known = {p["name"] for p in live}
        if hub.storage:
            for entry in hub.storage.all_publishers():
                if entry["name"] not in known and entry["manifest"].get("public"):
                    live.append({
                        "name": entry["name"],
                        "description": entry["manifest"].get("description", ""),
                        "operator": entry["manifest"].get("operator", ""),
                        "capabilities": [c.get("name") for c in entry["manifest"].get("capabilities", [])],
                        "connections": 0,
                        "uptime_seconds": 0,
                        "online": False,
                        "last_seen": entry["last_seen"],
                        "total_chats": entry["total_chats"],
                    })
        html = _render_registry_html(live)
        return HTMLResponse(html)

    @app.get("/registry")
    async def registry() -> JSONResponse:
        listings = []
        for name, p in hub.publishers.items():
            if p.manifest.get("public"):
                listings.append({
                    "name": name,
                    "description": p.manifest.get("description", ""),
                    "capabilities": [c.get("name") for c in p.manifest.get("capabilities", [])],
                    "manifest_url": f"/{name}/manifest.json",
                })
        return JSONResponse(listings)

    @app.get("/registry/global")
    async def registry_global() -> JSONResponse:
        """Local listings + peer-hub listings, annotated with origin.
        Peers come from ZHUB_PEERS env var (comma-separated URLs).
        Offline peers are silently skipped — never block the local response."""
        local: list[dict[str, Any]] = []
        for name, p in hub.publishers.items():
            if p.manifest.get("public"):
                local.append({
                    "name": name,
                    "description": p.manifest.get("description", ""),
                    "capabilities": [c.get("name") for c in p.manifest.get("capabilities", [])],
                    "manifest_url": f"/{name}/manifest.json",
                    "origin": "self",
                })
        peers_env = os.environ.get("ZHUB_PEERS", "")
        peers = [p.strip() for p in peers_env.split(",") if p.strip()]
        if peers:
            from .federation import PeerRegistry
            pr = PeerRegistry(peers)
            try:
                peer_entries = await pr.aggregate()
            finally:
                await pr.close()
            return JSONResponse(local + peer_entries)
        return JSONResponse(local)

    async def _find_peer_for(ai_name: str) -> Optional[str]:
        """Look across configured peers; return the first peer URL whose
        /registry lists ``ai_name``. Returns None if no peers know it."""
        peers_env = os.environ.get("ZHUB_PEERS", "")
        peers = [p.strip() for p in peers_env.split(",") if p.strip()]
        if not peers:
            return None
        try:
            import httpx
        except ImportError:
            return None
        async with httpx.AsyncClient(timeout=5.0) as client:
            for peer in peers:
                try:
                    resp = await client.get(peer.rstrip("/") + "/registry")
                    if resp.status_code != 200:
                        continue
                    entries = resp.json()
                    if not isinstance(entries, list):
                        continue
                    if any(e.get("name") == ai_name for e in entries):
                        return peer
                except Exception as e:
                    log.warning("peer registry check failed for %s: %s", peer, e)
        return None

    async def _proxy_to_peer(peer_url: str, ai_name: str, body: dict[str, Any],
                              api_key: str, forwarded_by_chain: list[str]):
        """Forward chat-completions to a peer hub. Streams or single-shot
        based on body['stream']. Returns Response with X-Zhub-Origin set."""
        try:
            import httpx
        except ImportError:
            raise HTTPException(500, "peer routing requires httpx")

        target = peer_url.rstrip("/") + f"/{ai_name}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}" if api_key else "",
            "X-Zhub-Forwarded-By": ",".join(forwarded_by_chain),
            "Content-Type": "application/json",
        }
        is_stream = bool(body.get("stream"))
        if is_stream:
            client = httpx.AsyncClient(timeout=60.0)

            async def upstream():
                try:
                    async with client.stream("POST", target, json=body, headers=headers) as resp:
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                finally:
                    await client.aclose()

            return StreamingResponse(
                upstream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "X-Zhub-Origin": peer_url,
                },
            )

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(target, json=body, headers=headers)
            except httpx.RequestError as e:
                raise HTTPException(502, f"peer hub unreachable: {e}")
        try:
            content = resp.json()
        except ValueError:
            content = {"error": {"code": "bad_peer_response", "message": resp.text[:200]}}
        return JSONResponse(
            status_code=resp.status_code,
            content=content,
            headers={"X-Zhub-Origin": peer_url},
        )

    async def _tunnel_ws_connect(client_ws: WebSocket, peer_url: str,
                                  initial_register_json: str) -> None:
        """Phase 1.1b: pump messages bidirectionally between an already-
        accepted local /ws/connect WebSocket and the peer's /ws/connect.

        ``initial_register_json`` is the (rewritten) register-connection
        envelope to send first — already accounts for the via chain. After
        that we shuttle every message verbatim until either side closes."""
        try:
            import websockets as _ws
        except ImportError:
            await client_ws.send_text(
                error_envelope("", "register_failed", "peer routing requires websockets").to_json()
            )
            return

        # Convert http://host:port → ws://host:port/ws/connect
        scheme_map = {"http": "ws", "https": "wss"}
        from urllib.parse import urlparse
        u = urlparse(peer_url)
        peer_ws_url = f"{scheme_map.get(u.scheme, 'ws')}://{u.netloc}/ws/connect"

        try:
            peer_ws = await _ws.connect(peer_ws_url, max_size=10_000_000)
        except Exception as e:
            try:
                await client_ws.send_text(
                    error_envelope("", "register_failed", f"peer unreachable: {e}").to_json()
                )
            except Exception:
                pass
            return

        try:
            await peer_ws.send(initial_register_json)
        except Exception as e:
            try:
                await client_ws.send_text(
                    error_envelope("", "register_failed", f"peer send failed: {e}").to_json()
                )
            finally:
                await peer_ws.close()
            return

        async def client_to_peer() -> None:
            try:
                while True:
                    msg = await client_ws.receive_text()
                    await peer_ws.send(msg)
            except Exception:
                pass

        async def peer_to_client() -> None:
            try:
                async for msg in peer_ws:
                    if isinstance(msg, bytes):
                        msg = msg.decode("utf-8", errors="replace")
                    await client_ws.send_text(msg)
            except Exception:
                pass

        try:
            done, pending = await asyncio.wait(
                [asyncio.create_task(client_to_peer()),
                 asyncio.create_task(peer_to_client())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
        finally:
            try:
                await peer_ws.close()
            except Exception:
                pass

    @app.get("/{ai_name}/manifest.json")
    async def manifest(ai_name: str) -> JSONResponse:
        publisher = hub.publishers.get(ai_name)
        if publisher is None:
            raise HTTPException(404, f"AI '{ai_name}' not registered")
        m = dict(publisher.manifest)
        m["endpoints"] = {
            "chat": f"/{ai_name}/v1/chat/completions",
            "manifest": f"/{ai_name}/manifest.json",
            "registry": "/registry",
        }
        m["connections"] = [
            {
                "connection_id": c.connection_id,
                "client_manifest": c.client_manifest,
                "connected_for_seconds": int(time.time() - c.created_at),
            }
            for c in hub.connections_by_ai.get(ai_name, {}).values()
        ]
        return JSONResponse(m)

    async def _run_autoresolve_loop(
        ai_name: str,
        initial_messages: list[dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        tools: Optional[list[dict[str, Any]]],
        tool_choice: Any,
        tool_resolve_mode: str,
        max_iters: int = 4,
    ) -> tuple[str, Optional[str]]:
        """Run the same tool-call auto-resolve loop the non-streaming chat
        path uses, returning (final_text, finish_reason). Used by Phase 4.2's
        pre-resolve streaming mode so streaming clients can opt into proper
        tool-call resolution. Side-effects: capability invocations + metrics
        bumps happen as the loop runs."""
        running_messages = list(initial_messages)
        iters = 0
        while True:
            try:
                response = await hub.proxy_chat(
                    ai_name, running_messages, model, temperature, max_tokens,
                    tools=tools or None, tool_choice=tool_choice,
                )
            except LookupError:
                raise HTTPException(404, "AI offline")
            except asyncio.TimeoutError:
                raise HTTPException(504, "AI did not respond in time")

            tool_calls = response.get("tool_calls") or []
            if not tool_calls or tool_resolve_mode == "client" or iters >= max_iters:
                return (response.get("text", "") or "",
                        response.get("finish_reason"))

            running_messages = running_messages + [{
                "role": "assistant",
                "content": response.get("text", "") or None,
                "tool_calls": tool_calls,
            }]

            async def _resolve_one(tc: Any) -> dict[str, Any]:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                cap_name = fn.get("name", "")
                args_raw = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except (ValueError, TypeError):
                    args = {}
                conn_id = hub.find_capability_connection(ai_name, cap_name)
                if conn_id is None:
                    tool_result: Any = {"error": f"capability '{cap_name}' not connected"}
                else:
                    schema = hub.find_capability_schema(ai_name, cap_name)
                    val_errors = validate_schema(args, schema) if schema else []
                    if val_errors:
                        return {
                            "tool_call_id": tc.get("id") if isinstance(tc, dict) else None,
                            "name": cap_name,
                            "args": args,
                            "result": {"error": "validation failed: " + "; ".join(val_errors)},
                        }
                    try:
                        relayed = await hub.invoke_capability(
                            ai_name, conn_id, cap_name, args,
                        )
                        if isinstance(relayed, dict) and "ok" in relayed:
                            if relayed.get("ok"):
                                tool_result = relayed.get("result")
                                if tool_result is None:
                                    tool_result = {"ok": True}
                            else:
                                tool_result = {"error": relayed.get("error") or "invoke failed"}
                        else:
                            tool_result = relayed
                    except Exception as e:
                        tool_result = {"error": str(e)}
                return {
                    "tool_call_id": tc.get("id") if isinstance(tc, dict) else None,
                    "name": cap_name,
                    "args": args,
                    "result": tool_result,
                }

            resolved = await asyncio.gather(*(_resolve_one(tc) for tc in tool_calls))
            for entry in resolved:
                hub.bump(ai_name, "tool_calls_resolved")
                running_messages.append({
                    "role": "tool",
                    "tool_call_id": entry["tool_call_id"],
                    "name": entry["name"],
                    "content": json.dumps(entry["result"]),
                })
            iters += 1

    @app.post("/{ai_name}/v1/invoke")
    async def invoke_capability_http(ai_name: str, request: Request):
        """Direct HTTP invocation of a connected client's capability.

        Companion to the existing OpenAI chat endpoint. Lets external
        callers (e.g. zhub.mcp_server) call any connected capability by
        name without having to round-trip through chat → tool_call.
        """
        body = await request.json()
        api_key_header = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if hub.lookup_by_api_key(api_key_header) != ai_name:
            raise HTTPException(401, "invalid api key for this AI")

        capability = body.get("capability") or ""
        args = body.get("args") or {}
        connection_id = body.get("connection_id")
        if not capability:
            raise HTTPException(400, "missing 'capability'")

        if connection_id:
            conn = hub.connections_by_ai.get(ai_name, {}).get(connection_id)
            if conn is None:
                raise HTTPException(404, f"connection '{connection_id}' not found")
            schema = next(
                (c.get("schema") for c in conn.client_manifest.get("capabilities", [])
                 if c.get("name") == capability),
                None,
            )
            if schema is None:
                raise HTTPException(404,
                    f"connection '{connection_id}' does not expose '{capability}'")
        else:
            connection_id = hub.find_capability_connection(ai_name, capability)
            if connection_id is None:
                raise HTTPException(404,
                    f"capability '{capability}' not available on any connection")
            schema = hub.find_capability_schema(ai_name, capability)

        if isinstance(schema, dict):
            errs = validate_schema(args, schema)
            if errs:
                raise HTTPException(400, "validation failed: " + "; ".join(errs))

        try:
            relayed = await hub.invoke_capability(ai_name, connection_id, capability, args)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except asyncio.TimeoutError:
            raise HTTPException(504, "capability did not respond in time")

        if isinstance(relayed, dict) and "ok" in relayed:
            if relayed.get("ok"):
                result = relayed.get("result")
            else:
                return JSONResponse(
                    status_code=502,
                    content={"ok": False, "error": relayed.get("error") or "invoke failed"},
                )
        else:
            result = relayed
        hub.bump(ai_name, "http_invoke")
        return JSONResponse({"ok": True, "result": result, "connection_id": connection_id})

    @app.post("/{ai_name}/v1/chat/completions")
    async def chat_completions(ai_name: str, request: Request):
        body = await request.json()
        api_key_header = request.headers.get("authorization", "").removeprefix("Bearer ").strip()

        # Loop-prevention: refuse if our own hub_id already in the chain.
        forwarded_by = request.headers.get("x-zhub-forwarded-by", "")
        chain = [s.strip() for s in forwarded_by.split(",") if s.strip()]
        if hub.hub_id in chain:
            return JSONResponse(
                status_code=508,
                content={"error": {"code": "loop_detected",
                                   "message": f"hub {hub.hub_id} already in forwarding chain"}},
            )

        # AI not registered locally → try peer routing before 401-ing on auth.
        if ai_name not in hub.publishers:
            peer_url = await _find_peer_for(ai_name)
            if peer_url is None:
                raise HTTPException(404, f"AI '{ai_name}' not found locally or in peers")
            hub.bump(ai_name, "peer_proxied")
            return await _proxy_to_peer(
                peer_url, ai_name, body, api_key_header,
                forwarded_by_chain=chain + [hub.hub_id],
            )

        if hub.lookup_by_api_key(api_key_header) != ai_name:
            raise HTTPException(401, "invalid api key for this AI")

        # Rate-limit enforcement (Phase 1.7)
        rl_ok, retry_after = hub.check_rate_limit(ai_name, api_key_header)
        if not rl_ok:
            hub.bump(ai_name, "rate_limited")
            ra = max(1, int(round(retry_after or 1.0)))
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": "rate limit exceeded for this api key",
                        "retry_after": ra,
                    },
                },
                headers={"Retry-After": str(ra)},
            )

        hub.bump(ai_name, "chat_requests")
        messages = body.get("messages", [])
        model = body.get("model", "default")
        temperature = float(body.get("temperature", 0.4))
        max_tokens = int(body.get("max_tokens", 4096))
        stream = bool(body.get("stream", False))
        caller_tools = body.get("tools") if isinstance(body.get("tools"), list) else None
        merged_tools = hub.build_tools_for(ai_name, caller_tools)
        tool_choice = body.get("tool_choice")

        if stream:
            stream_tools_mode = request.headers.get("x-zhub-stream-tools", "").lower()
            if stream_tools_mode == "pre-resolve":
                # Phase 4.2: run the full non-streaming auto-resolve loop,
                # then emit the final text as a single SSE chunk + done.
                # Trades stream-latency for tool-call correctness when the
                # client has opted in via header.
                tool_resolve_mode = request.headers.get("x-zhub-tool-resolve", "auto").lower()
                final_text, final_finish = await _run_autoresolve_loop(
                    ai_name=ai_name,
                    initial_messages=messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=merged_tools or None,
                    tool_choice=tool_choice,
                    tool_resolve_mode=tool_resolve_mode,
                )

                async def preresolved_stream():
                    created = int(time.time())
                    completion_id = "chatcmpl-" + new_request_id()[:16]
                    sse = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"role": "assistant", "content": final_text},
                            "finish_reason": None,
                        }],
                    }
                    yield f"data: {json.dumps(sse)}\n\n"
                    sse_done = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {},
                            "finish_reason": final_finish or "stop",
                        }],
                    }
                    yield f"data: {json.dumps(sse_done)}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(
                    preresolved_stream(),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )

            try:
                response = await hub.proxy_chat(
                    ai_name, messages, model, temperature, max_tokens, stream=True,
                    tools=merged_tools or None, tool_choice=tool_choice,
                )
            except LookupError:
                raise HTTPException(404, "AI offline")

            queue: asyncio.Queue = response["_stream_queue"]
            request_id = response["_request_id"]

            async def event_stream():
                created = int(time.time())
                completion_id = "chatcmpl-" + new_request_id()[:16]
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    delta_text = chunk.get("delta", "")
                    done = chunk.get("done", False)
                    finish_reason = chunk.get("finish_reason")
                    if done:
                        # final chunk per OpenAI streaming spec
                        sse = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {},
                                "finish_reason": finish_reason or "stop",
                            }],
                        }
                        yield f"data: {json.dumps(sse)}\n\n"
                        yield "data: [DONE]\n\n"
                        break
                    sse = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"role": "assistant", "content": delta_text},
                            "finish_reason": None,
                        }],
                    }
                    yield f"data: {json.dumps(sse)}\n\n"

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # Tool-call auto-resolution loop (Phase 1.8). When the publisher
        # returns tool_calls and the operator hasn't opted out via
        # X-Zhub-Tool-Resolve: client, the hub invokes the matching
        # capability on a connected client, appends a role=tool message
        # with the result, and re-asks the publisher. Bounded by max_tool_iters
        # to prevent runaway recursion.
        tool_resolve_mode = request.headers.get("x-zhub-tool-resolve", "auto").lower()
        max_tool_iters = 4
        iters = 0
        running_messages = list(messages)
        tool_audit: list[dict[str, Any]] = []
        while True:
            try:
                response = await hub.proxy_chat(
                    ai_name, running_messages, model, temperature, max_tokens,
                    tools=merged_tools or None, tool_choice=tool_choice,
                )
            except LookupError:
                raise HTTPException(404, "AI offline")
            except asyncio.TimeoutError:
                raise HTTPException(504, "AI did not respond in time")

            tool_calls = response.get("tool_calls") or []
            if not tool_calls or tool_resolve_mode == "client" or iters >= max_tool_iters:
                break

            # Auto-resolve: map each tool call to a connection capability,
            # invoke all in parallel (real LLMs frequently emit several at
            # once), then thread the results back in deterministic order.
            running_messages = running_messages + [{
                "role": "assistant",
                "content": response.get("text", "") or None,
                "tool_calls": tool_calls,
            }]

            async def _resolve_one(tc: Any) -> dict[str, Any]:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                cap_name = fn.get("name", "")
                args_raw = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except (ValueError, TypeError):
                    args = {}
                conn_id = hub.find_capability_connection(ai_name, cap_name)
                if conn_id is None:
                    tool_result: Any = {"error": f"capability '{cap_name}' not connected"}
                else:
                    schema = hub.find_capability_schema(ai_name, cap_name)
                    val_errors = validate_schema(args, schema) if schema else []
                    if val_errors:
                        tool_result = {"error": "validation failed: " + "; ".join(val_errors)}
                        return {
                            "tool_call_id": tc.get("id") if isinstance(tc, dict) else None,
                            "name": cap_name,
                            "args": args,
                            "result": tool_result,
                        }
                    try:
                        relayed = await hub.invoke_capability(
                            ai_name, conn_id, cap_name, args,
                        )
                        if isinstance(relayed, dict) and "ok" in relayed:
                            if relayed.get("ok"):
                                tool_result = relayed.get("result")
                                if tool_result is None:
                                    tool_result = {"ok": True}
                            else:
                                tool_result = {"error": relayed.get("error") or "invoke failed"}
                        else:
                            tool_result = relayed
                    except Exception as e:
                        tool_result = {"error": str(e)}
                return {
                    "tool_call_id": tc.get("id") if isinstance(tc, dict) else None,
                    "name": cap_name,
                    "args": args,
                    "result": tool_result,
                }

            resolved = await asyncio.gather(*(_resolve_one(tc) for tc in tool_calls))
            for entry in resolved:
                tool_audit.append(entry)
                hub.bump(ai_name, "tool_calls_resolved")
                running_messages.append({
                    "role": "tool",
                    "tool_call_id": entry["tool_call_id"],
                    "name": entry["name"],
                    "content": json.dumps(entry["result"]),
                })
            iters += 1

        # Wrap into OpenAI-style response shape (non-streaming)
        text = response.get("text", "")
        usage = dict(response.get("usage") or {})
        if tool_audit:
            usage["tool_results"] = tool_audit
        message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_calls and tool_resolve_mode == "client":
            message["tool_calls"] = tool_calls
            finish_reason = response.get("finish_reason", "tool_calls")
        else:
            finish_reason = response.get("finish_reason", "stop")
        return JSONResponse({
            "id": "chatcmpl-" + new_request_id()[:16],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }],
            "usage": usage,
        })

    @app.websocket("/ws/publish")
    async def ws_publish(websocket: WebSocket) -> None:
        """A publisher (AI) connects here. Long-lived. After register-publisher,
        the hub may push chat-request envelopes that the publisher must reply to."""
        await websocket.accept()
        ai_name: Optional[str] = None
        try:
            while True:
                text = await websocket.receive_text()
                env = Envelope.from_json(text)
                if env.type == "register-publisher" and ai_name is None:
                    desired = env.payload.get("desired_name") or env.payload.get("manifest", {}).get("name", "ai")
                    desired_key = env.payload.get("api_key")  # for re-registration
                    try:
                        name, api_key = await hub.register_publisher(
                            desired, env.payload.get("manifest", {}), websocket,
                            desired_api_key=desired_key,
                        )
                    except PermissionError as e:
                        await websocket.send_text(
                            error_envelope(env.request_id, "register_failed", str(e)).to_json()
                        )
                        break
                    ai_name = name
                    base_url = "/" + name
                    await websocket.send_text(registered(name, base_url, api_key).to_json())

                elif env.type == "chat-response" and ai_name:
                    publisher = hub.publishers.get(ai_name)
                    if publisher and env.request_id in publisher.pending:
                        target = publisher.pending[env.request_id]
                        if isinstance(target, asyncio.Queue):
                            # streaming caller — convert non-streaming response to single chunk
                            await target.put({"delta": env.payload.get("text", ""),
                                              "done": False})
                            await target.put({"done": True,
                                              "finish_reason": env.payload.get("finish_reason", "stop")})
                            await target.put(None)
                        else:
                            target.set_result(env.payload)
                    elif env.request_id in hub.client_routes:
                        # ws_connect client is awaiting — relay
                        client_ws, _ = hub.client_routes.pop(env.request_id)
                        try:
                            await client_ws.send_text(env.to_json())
                        except Exception:
                            pass

                elif env.type == "chat-chunk" and ai_name:
                    publisher = hub.publishers.get(ai_name)
                    if publisher and env.request_id in publisher.pending:
                        target = publisher.pending[env.request_id]
                        if isinstance(target, asyncio.Queue):
                            await target.put(env.payload)
                            if env.payload.get("done"):
                                await target.put(None)
                    elif env.request_id in hub.client_routes:
                        client_ws, _ = hub.client_routes[env.request_id]
                        try:
                            await client_ws.send_text(env.to_json())
                        except Exception:
                            hub.client_routes.pop(env.request_id, None)
                        if env.payload.get("done"):
                            hub.client_routes.pop(env.request_id, None)

                elif env.type == "invoke-request" and ai_name:
                    # Publisher wants to call a connected client.
                    conn_id = env.payload.get("connection_id")
                    capability = env.payload.get("capability")
                    args = env.payload.get("args", {})
                    try:
                        # The connection's invoke-result envelope payload already
                        # carries {ok, result, error}. Forward it as-is — don't
                        # re-wrap, otherwise publishers receive double-nested
                        # {ok:true, result:{ok:..., result:..., error:...}}.
                        relayed = await hub.invoke_capability(ai_name, conn_id, capability, args)
                        await websocket.send_text(
                            Envelope(
                                type="invoke-result",
                                request_id=env.request_id,
                                payload=relayed,
                            ).to_json()
                        )
                    except Exception as e:
                        await websocket.send_text(
                            invoke_result(env.request_id, ok=False, error=str(e)).to_json()
                        )

                elif env.type == "ping":
                    await websocket.send_text(Envelope(type="pong", request_id=env.request_id).to_json())

                elif env.type == "unregister" and ai_name:
                    break

        except WebSocketDisconnect:
            pass
        finally:
            if ai_name:
                await hub.unregister_publisher(ai_name)

    @app.websocket("/ws/connect")
    async def ws_connect(websocket: WebSocket) -> None:
        """A client connecting to a publisher. Long-lived. Receives invoke-request
        from the AI and returns invoke-result."""
        await websocket.accept()
        ai_name: Optional[str] = None
        connection_id: Optional[str] = None
        try:
            while True:
                text = await websocket.receive_text()
                env = Envelope.from_json(text)

                if env.type == "register-connection" and ai_name is None:
                    payload_ai = env.payload.get("ai_name")
                    api_key = env.payload.get("api_key", "")
                    client_manifest = env.payload.get("client_manifest", {})

                    # Phase 1.1b: peer-route this WS if AI lives on a peer hub.
                    if payload_ai and payload_ai not in hub.publishers:
                        via = env.payload.get("via") or []
                        if hub.hub_id in via:
                            await websocket.send_text(
                                error_envelope(env.request_id, "register_failed",
                                               f"loop: {hub.hub_id} already in via").to_json()
                            )
                            break
                        peer_url = await _find_peer_for(payload_ai)
                        if peer_url is not None:
                            new_payload = dict(env.payload)
                            new_payload["via"] = list(via) + [hub.hub_id]
                            new_env = Envelope(type="register-connection",
                                               request_id=env.request_id,
                                               payload=new_payload)
                            await _tunnel_ws_connect(websocket, peer_url, new_env.to_json())
                            return  # tunnel handled the rest of the session

                    try:
                        connection_id = await hub.register_connection(
                            payload_ai, api_key, client_manifest, websocket,
                        )
                        ai_name = payload_ai
                        await websocket.send_text(
                            registered(payload_ai, f"/{payload_ai}", None).to_json()
                        )
                    except (ValueError, PermissionError) as e:
                        await websocket.send_text(
                            error_envelope(env.request_id, "register_failed", str(e)).to_json()
                        )
                        break

                elif env.type == "chat-request" and ai_name:
                    # Forward as-is to the publisher (preserves stream:true).
                    # Hub routes chat-response or chat-chunk back via client_routes.
                    publisher = hub.publishers.get(ai_name)
                    if publisher is None:
                        await websocket.send_text(
                            error_envelope(env.request_id, "ai_offline", "AI not registered").to_json()
                        )
                    else:
                        hub.client_routes[env.request_id] = (websocket, ai_name)
                        try:
                            await publisher.websocket.send_text(env.to_json())
                        except Exception as e:
                            hub.client_routes.pop(env.request_id, None)
                            await websocket.send_text(
                                error_envelope(env.request_id, "chat_failed", str(e)).to_json()
                            )

                elif env.type == "invoke-result" and ai_name and connection_id:
                    # The client is returning the result of an invoke from the AI
                    conn = hub.connections_by_ai.get(ai_name, {}).get(connection_id)
                    if conn and env.request_id in conn.pending:
                        conn.pending[env.request_id].set_result(env.payload)

                elif env.type == "ping":
                    await websocket.send_text(Envelope(type="pong", request_id=env.request_id).to_json())

                elif env.type == "unregister":
                    break

        except WebSocketDisconnect:
            pass
        finally:
            if ai_name and connection_id:
                await hub.unregister_connection(ai_name, connection_id)

    app.state.hub = hub
    return app


def _render_registry_html(entries: list[dict[str, Any]]) -> str:
    """Tiny inline registry page. Mobile-friendly. Auto-refresh."""
    rows_html = []
    for e in entries:
        status_color = "#39FF7A" if e["online"] else "#7A9C82"
        status_label = "online" if e["online"] else "offline"
        caps_html = ", ".join(e.get("capabilities") or [])
        conn_label = f"{e['connections']} connection(s)" if e["online"] else "—"
        rows_html.append(f"""
        <tr>
          <td><span style="color:{status_color}">●</span> {status_label}</td>
          <td><a href="/{e['name']}/manifest.json">{e['name']}</a></td>
          <td>{e['description'] or '—'}</td>
          <td><small>{caps_html}</small></td>
          <td>{conn_label}</td>
        </tr>
        """)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="10">
  <title>zhub registry</title>
  <style>
    body {{ background:#0A0E0A; color:#D9F2DA; font-family: ui-monospace,SFMono-Regular,Menlo,monospace; padding:16px; }}
    h1 {{ color:#39FF7A; margin:0 0 4px 0; font-size:1.4rem; }}
    .tag {{ color:#FFB400; }}
    table {{ width:100%; border-collapse: collapse; margin-top:16px; }}
    th, td {{ padding:6px 10px; text-align:left; vertical-align:top; }}
    th {{ color:#7A9C82; border-bottom:1px solid #1F2D24; font-weight:normal; font-size:0.85rem; }}
    tr td {{ border-bottom:1px solid #152018; }}
    a {{ color:#5AC8FA; text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
    small {{ color:#7A9C82; }}
    footer {{ margin-top:24px; color:#4A6B53; font-size:0.8rem; }}
  </style>
</head>
<body>
  <h1>zhub <span class="tag">registry</span></h1>
  <small>WiFi for AIs · {len(entries)} listed · auto-refresh 10s</small>
  <table>
    <thead><tr><th>status</th><th>name</th><th>description</th><th>capabilities</th><th>activity</th></tr></thead>
    <tbody>{''.join(rows_html) if rows_html else '<tr><td colspan="5"><small>no public AIs registered yet — be the first.</small></td></tr>'}</tbody>
  </table>
  <footer>
    <a href="/registry">/registry</a> · <a href="/healthz">/healthz</a> · zhub v0.1.0
  </footer>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="zhub hub server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--public-tunnel",
        action="store_true",
        help="Spawn an ephemeral Cloudflare Tunnel via `cloudflared` and print the public URL.",
    )
    parser.add_argument(
        "--db",
        default="zhub.db",
        help="SQLite path for persistent publisher registry. Pass empty string to disable.",
    )
    parser.add_argument(
        "--peers",
        default="",
        help="Comma-separated peer hub URLs for read-only federation. "
             "Peer registries surface at /registry/global with origin annotation.",
    )
    args = parser.parse_args()
    db_path: Optional[str] = args.db if args.db else None
    if args.peers:
        os.environ["ZHUB_PEERS"] = args.peers

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.public_tunnel:
        from .tunnel import CloudflareTunnel
        if not CloudflareTunnel.is_available():
            print(
                "warning: --public-tunnel requested but `cloudflared` not found on PATH.\n"
                "         install: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/\n"
                "         falling back to localhost-only mode."
            )
        else:
            async def _run_with_tunnel() -> None:
                tunnel = CloudflareTunnel(local_port=args.port)
                try:
                    url = await tunnel.start()
                    print()
                    print("=" * 60)
                    print(f"  zhub public URL:  {url}")
                    print("=" * 60)
                    print()
                    config = uvicorn.Config(
                        create_app(db_path=db_path), host=args.host, port=args.port,
                        log_level=args.log_level,
                    )
                    server = uvicorn.Server(config)
                    await server.serve()
                finally:
                    await tunnel.close()
            asyncio.run(_run_with_tunnel())
            return

    app = create_app(db_path=db_path)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
