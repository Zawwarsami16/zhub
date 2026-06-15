"""CohereAdapter — wraps Cohere v2 chat completions API.

Cohere's wire shape is its own (not OpenAI-compat). Streaming responses
are line-delimited JSON objects with a `type` discriminator:

  {"type": "message-start", ...}
  {"type": "content-delta", "delta": {"message": {"content": {"text": "Hi"}}}}
  {"type": "content-delta", "delta": {"message": {"content": {"text": " there"}}}}
  {"type": "message-end", "delta": {"finish_reason": "complete"}}

Probe: GET https://api.cohere.com/v1/models, 1.5s timeout.
Env:   COHERE_API_KEY  (required)
       COHERE_MODEL    (default command-r-plus-08-2024)
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Optional

import httpx

from .base import BrainAdapter, ChatChunk


_PROBE_TIMEOUT = 1.5
_BASE_URL = "https://api.cohere.com"
_DEFAULT_MODEL = "command-r-plus-08-2024"


class CohereAdapter(BrainAdapter):
    name = "cohere"
    label = "Cohere Command-R+"
    env_keys = ("COHERE_API_KEY",)

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
    def try_init(cls) -> Optional["CohereAdapter"]:
        key = os.environ.get("COHERE_API_KEY")
        if not key:
            return None
        try:
            r = httpx.get(
                f"{_BASE_URL}/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=_PROBE_TIMEOUT,
            )
            if r.status_code != 200:
                return None
        except Exception:
            return None
        model = os.environ.get("COHERE_MODEL") or _DEFAULT_MODEL
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
        msgs: list[dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with self._http.stream(
            "POST", f"{_BASE_URL}/v2/chat", json=body, headers=headers,
        ) as response:
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                # Cohere v2 uses raw newline-delimited JSON (no "data:" prefix)
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = data.get("type")
                if kind == "content-delta":
                    delta = data.get("delta") or {}
                    msg = delta.get("message") or {}
                    content = msg.get("content") or {}
                    text = content.get("text") or ""
                    if text:
                        yield ChatChunk(delta=text, done=False, raw=data)
                elif kind == "message-end":
                    delta = data.get("delta") or {}
                    finish = delta.get("finish_reason") or "stop"
                    # Cohere uses "complete"; normalize to "stop"
                    if finish == "complete":
                        finish = "stop"
                    yield ChatChunk(
                        delta="", done=True,
                        finish_reason=finish, raw=data,
                    )
                    return
