"""AnthropicAdapter — wraps Anthropic's Messages API.

Probe: GET https://api.anthropic.com/v1/models with x-api-key header,
       1.5s timeout.
Stream: POST /v1/messages with stream=true. Anthropic's SSE shape is
        a sequence of (event, data) pairs; the deltas we surface come
        from `content_block_delta` events with `text_delta` payloads.
        `message_delta.stop_reason` carries the finish reason.
Env:    ANTHROPIC_API_KEY  (required)
        ANTHROPIC_MODEL    (default claude-sonnet-4-5)
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Optional

import httpx

from .base import BrainAdapter, ChatChunk


_PROBE_TIMEOUT = 1.5
_BASE_URL = "https://api.anthropic.com/v1"
_DEFAULT_MODEL = "claude-sonnet-4-5"
_API_VERSION = "2023-06-01"


class AnthropicAdapter(BrainAdapter):
    name = "anthropic"
    label = "Anthropic Claude"

    def __init__(
        self,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        http: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self._http = http or httpx.AsyncClient(timeout=120.0)

    @classmethod
    def try_init(cls) -> Optional["AnthropicAdapter"]:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        try:
            r = httpx.get(
                f"{_BASE_URL}/models",
                headers={
                    "x-api-key": key,
                    "anthropic-version": _API_VERSION,
                },
                timeout=_PROBE_TIMEOUT,
            )
            if r.status_code != 200:
                return None
        except Exception:
            return None
        model = os.environ.get("ANTHROPIC_MODEL") or _DEFAULT_MODEL
        return cls(api_key=key, model=model)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> AsyncIterator[ChatChunk]:
        # Anthropic Messages API expects system as a top-level field;
        # the messages list must alternate user/assistant.
        non_system = [m for m in messages if m.get("role") != "system"]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": non_system,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if system:
            body["system"] = system
        # Anthropic accepts tools in OpenAI-like shape under `tools`
        if tools:
            body["tools"] = tools
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }
        finish_reason: Optional[str] = None
        async with self._http.stream(
            "POST", f"{_BASE_URL}/messages", json=body, headers=headers,
        ) as response:
            # Non-2xx (429 overloaded, 401 bad key, 5xx) returns a JSON error
            # body, not SSE, so the loop below would skip every line and end
            # yielding nothing. Raise so the publisher surfaces a real error
            # instead of a silent empty completion. getattr default keeps test
            # doubles working; real httpx responses always have status_code.
            status = getattr(response, "status_code", 200)
            if status >= 400:
                try:
                    detail = (await response.aread()).decode("utf-8", "replace").strip()
                except Exception:
                    detail = ""
                snippet = f": {detail[:300]}" if detail else ""
                raise RuntimeError(f"upstream returned HTTP {status}{snippet}")
            async for line in response.aiter_lines():
                line = line.rstrip("\r")
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                kind = data.get("type")
                if kind == "content_block_delta":
                    delta_obj = data.get("delta") or {}
                    if delta_obj.get("type") == "text_delta":
                        yield ChatChunk(
                            delta=delta_obj.get("text", ""),
                            done=False,
                            raw=data,
                        )
                elif kind == "message_delta":
                    fr = (data.get("delta") or {}).get("stop_reason")
                    if fr:
                        finish_reason = fr
                elif kind == "message_stop":
                    yield ChatChunk(
                        delta="", done=True,
                        finish_reason=finish_reason or "end_turn",
                        raw=data,
                    )
                    return
