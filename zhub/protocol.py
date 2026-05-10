"""
Wire protocol between hub and (publish/connect) clients.

WebSocket-based JSON messages. Both directions use the same shape — a
typed envelope with `type` discriminator. Hub multiplexes traffic over a
single long-lived connection per client.

Message types:

    PUBLISHER → HUB
        register-publisher    AI announces itself, sends manifest
        chat-response         AI returns a chat completion to a request
        invoke-request        AI wants to call back to a connected client
        invoke-result         (when AI itself is target of an invoke from elsewhere)
        ping                  keep-alive
        unregister            graceful shutdown

    CLIENT → HUB
        register-connection   client announces itself, sends client-manifest
        chat-request          client sends a chat completion request
        invoke-result         client returns the result of an invoke from AI
        ping
        unregister

    HUB → PUBLISHER
        chat-request          a chat call arrived for this AI
        invoke-result         a connected client returned the result of AI's invoke
        connection-event      a client connected/disconnected/updated capabilities
        registered            ack with assigned subdomain + key (on register-publisher)
        error

    HUB → CLIENT
        chat-response         the AI's response to client's chat-request
        invoke-request        the AI wants this client to perform a capability
        registered            ack
        error

A `request_id` correlates request/response pairs so multiple in-flight calls
don't interfere.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional, Literal
import json
import uuid


def new_request_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Envelope:
    type: str
    request_id: str = field(default_factory=new_request_id)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "Envelope":
        d = json.loads(text)
        return cls(type=d["type"], request_id=d.get("request_id", new_request_id()),
                   payload=d.get("payload", {}))


# Helper constructors so callers don't have to remember exact shapes.

def register_publisher(manifest: dict[str, Any], desired_name: Optional[str] = None) -> Envelope:
    return Envelope(
        type="register-publisher",
        payload={"manifest": manifest, "desired_name": desired_name},
    )


def register_connection(name: str, api_key: str, client_manifest: dict[str, Any]) -> Envelope:
    return Envelope(
        type="register-connection",
        payload={
            "ai_name": name,
            "api_key": api_key,
            "client_manifest": client_manifest,
        },
    )


def register_exposure(name: str, manifest: dict[str, Any],
                      device_key: Optional[str] = None) -> Envelope:
    """Phase 7.0: device announces capabilities WITHOUT pairing to an AI.
    Hub mints a `dx_` device key and `ex_` exposure id on first register;
    re-registration with the same device_key restores the same exposure_id."""
    return Envelope(
        type="register-exposure",
        payload={
            "name": name,
            "manifest": manifest,
            "device_key": device_key,
        },
    )


def exposure_registered(exposure_id: str, device_key: str, name: str) -> Envelope:
    return Envelope(
        type="exposure-registered",
        payload={
            "exposure_id": exposure_id,
            "device_key": device_key,
            "name": name,
        },
    )


def chat_request(messages: list[dict[str, Any]], model: str = "default",
                 temperature: float = 0.4, max_tokens: int = 4096,
                 extras: Optional[dict[str, Any]] = None) -> Envelope:
    payload: dict[str, Any] = {
        "messages": messages,
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if extras:
        payload.update(extras)
    return Envelope(type="chat-request", payload=payload)


def chat_response(text: str, request_id: str, finish_reason: str = "stop",
                  tool_calls: Optional[list[dict[str, Any]]] = None,
                  usage: Optional[dict[str, Any]] = None) -> Envelope:
    return Envelope(
        type="chat-response",
        request_id=request_id,
        payload={
            "text": text,
            "finish_reason": finish_reason,
            "tool_calls": tool_calls or [],
            "usage": usage or {},
        },
    )


def chat_chunk(delta: str, request_id: str, done: bool = False,
               finish_reason: Optional[str] = None) -> Envelope:
    """Streaming chunk — incremental delta of a chat response.

    The publisher sends one or more chat-chunk envelopes followed by a final
    chunk with done=True. The hub forwards them to the HTTP client as SSE
    events in OpenAI streaming format.
    """
    return Envelope(
        type="chat-chunk",
        request_id=request_id,
        payload={
            "delta": delta,
            "done": done,
            "finish_reason": finish_reason,
        },
    )


def invoke_request(connection_id: str, capability: str,
                   args: dict[str, Any]) -> Envelope:
    return Envelope(
        type="invoke-request",
        payload={
            "connection_id": connection_id,
            "capability": capability,
            "args": args,
        },
    )


def invoke_result(request_id: str, ok: bool, result: Any = None,
                  error: Optional[str] = None) -> Envelope:
    return Envelope(
        type="invoke-result",
        request_id=request_id,
        payload={"ok": ok, "result": result, "error": error},
    )


def connection_event(kind: Literal["connected", "disconnected", "updated"],
                     connection_id: str, client_manifest: Optional[dict[str, Any]] = None) -> Envelope:
    return Envelope(
        type="connection-event",
        payload={
            "kind": kind,
            "connection_id": connection_id,
            "client_manifest": client_manifest,
        },
    )


def registered(name: str, base_url: str, api_key: Optional[str] = None) -> Envelope:
    return Envelope(
        type="registered",
        payload={
            "name": name,
            "base_url": base_url,
            "api_key": api_key,
        },
    )


def error_envelope(request_id: str, code: str, message: str) -> Envelope:
    return Envelope(
        type="error",
        request_id=request_id,
        payload={"code": code, "message": message},
    )


def ping() -> Envelope:
    return Envelope(type="ping", payload={})
