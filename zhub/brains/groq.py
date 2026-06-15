"""GroqAdapter — wraps Groq's OpenAI-compatible API.

Probe: GET https://api.groq.com/openai/v1/models with bearer key, 1.5s timeout.
Stream: POST /openai/v1/chat/completions with stream=true.
Env:    GROQ_API_KEY   (required)
        GROQ_MODEL     (default llama-3.3-70b-versatile)
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Optional

import httpx

from .base import BrainAdapter, ChatChunk
from ._openai_compat import probe_openai_compat, stream_openai_compat


_BASE_URL = "https://api.groq.com/openai/v1"
_DEFAULT_MODEL = "llama-3.3-70b-versatile"


class GroqAdapter(BrainAdapter):
    name = "groq"
    label = "Groq Llama 3.3 70B"
    env_keys = ("GROQ_API_KEY",)

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
    def try_init(cls) -> Optional["GroqAdapter"]:
        key = os.environ.get("GROQ_API_KEY")
        if not key or not probe_openai_compat(_BASE_URL, key):
            return None
        model = os.environ.get("GROQ_MODEL") or _DEFAULT_MODEL
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
        async for chunk in stream_openai_compat(
            self._http, _BASE_URL, self.api_key, self.model, messages,
            system=system, temperature=temperature,
            max_tokens=max_tokens, tools=tools,
        ):
            yield chunk
