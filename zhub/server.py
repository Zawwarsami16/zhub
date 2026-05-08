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
import asyncio
import json
import logging
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse
    import uvicorn
except ImportError as e:
    raise SystemExit(
        "zhub.server requires fastapi + uvicorn. install with:\n"
        "    pip install 'fastapi>=0.110' 'uvicorn[standard]>=0.27'"
    ) from e

from .protocol import (
    Envelope, register_publisher, registered, chat_request, chat_response,
    invoke_request, invoke_result, connection_event, error_envelope, new_request_id,
)


log = logging.getLogger("zhub.server")


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
    def __init__(self) -> None:
        self.publishers: dict[str, PublisherRegistration] = {}
        self.connections_by_ai: dict[str, dict[str, ConnectionRegistration]] = defaultdict(dict)
        self.api_keys: dict[str, str] = {}  # api_key -> ai_name (for fast lookup)
        self.lock = asyncio.Lock()

    # publishers --------------------------------------------------------

    async def register_publisher(self, name: str, manifest: dict[str, Any],
                                 websocket: WebSocket) -> tuple[str, str]:
        """Register an AI. Returns (assigned_name, api_key)."""
        async with self.lock:
            assigned = name
            i = 1
            while assigned in self.publishers:
                i += 1
                assigned = f"{name}-{i}"
            api_key = "zk_" + secrets.token_urlsafe(24)
            api_key_hash = _hash_key(api_key)
            self.publishers[assigned] = PublisherRegistration(
                name=assigned,
                manifest=manifest,
                websocket=websocket,
                api_key_hash=api_key_hash,
            )
            self.api_keys[api_key] = assigned
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

    async def proxy_chat(self, ai_name: str, messages: list[dict[str, Any]],
                         model: str, temperature: float, max_tokens: int,
                         timeout: float = 60.0) -> dict[str, Any]:
        """Route an HTTP chat request to the publisher and await its response."""
        publisher = self.publishers.get(ai_name)
        if publisher is None:
            raise LookupError("publisher not registered")
        env = chat_request(messages=messages, model=model,
                           temperature=temperature, max_tokens=max_tokens)
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


def _hash_key(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()


# ---- FastAPI app --------------------------------------------------------

def create_app() -> FastAPI:
    hub = Hub()
    app = FastAPI(title="zhub", version="0.1.0")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "publishers": str(len(hub.publishers))}

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

    @app.post("/{ai_name}/v1/chat/completions")
    async def chat_completions(ai_name: str, request: Request) -> JSONResponse:
        body = await request.json()
        api_key_header = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if hub.lookup_by_api_key(api_key_header) != ai_name:
            raise HTTPException(401, "invalid api key for this AI")

        messages = body.get("messages", [])
        model = body.get("model", "default")
        temperature = float(body.get("temperature", 0.4))
        max_tokens = int(body.get("max_tokens", 4096))
        try:
            response = await hub.proxy_chat(ai_name, messages, model, temperature, max_tokens)
        except LookupError:
            raise HTTPException(404, "AI offline")
        except asyncio.TimeoutError:
            raise HTTPException(504, "AI did not respond in time")

        # Wrap into OpenAI-style response shape
        text = response.get("text", "")
        usage = response.get("usage", {})
        return JSONResponse({
            "id": "chatcmpl-" + new_request_id()[:16],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": response.get("finish_reason", "stop"),
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
                    name, api_key = await hub.register_publisher(
                        desired, env.payload.get("manifest", {}), websocket,
                    )
                    ai_name = name
                    base_url = "/" + name
                    await websocket.send_text(registered(name, base_url, api_key).to_json())

                elif env.type == "chat-response" and ai_name:
                    publisher = hub.publishers.get(ai_name)
                    if publisher and env.request_id in publisher.pending:
                        publisher.pending[env.request_id].set_result(env.payload)

                elif env.type == "invoke-request" and ai_name:
                    # Publisher wants to call a connected client.
                    conn_id = env.payload.get("connection_id")
                    capability = env.payload.get("capability")
                    args = env.payload.get("args", {})
                    try:
                        result = await hub.invoke_capability(ai_name, conn_id, capability, args)
                        await websocket.send_text(
                            invoke_result(env.request_id, ok=True, result=result).to_json()
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
                    # Client is sending a chat request — proxy through the hub
                    try:
                        result = await hub.proxy_chat(
                            ai_name,
                            env.payload.get("messages", []),
                            env.payload.get("model", "default"),
                            float(env.payload.get("temperature", 0.4)),
                            int(env.payload.get("max_tokens", 4096)),
                        )
                        await websocket.send_text(
                            chat_response(
                                text=result.get("text", ""),
                                request_id=env.request_id,
                                finish_reason=result.get("finish_reason", "stop"),
                                usage=result.get("usage"),
                            ).to_json()
                        )
                    except Exception as e:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="zhub hub server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
