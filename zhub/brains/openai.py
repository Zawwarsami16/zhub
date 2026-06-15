"""OpenAIAdapter — wraps OpenAI's chat completions API.

Probe: GET https://api.openai.com/v1/models, 1.5s timeout.
Stream: POST /v1/chat/completions with stream=true.
Env:    OPENAI_API_KEY  (required)
        OPENAI_MODEL    (default gpt-4o-mini)
        OPENAI_BASE_URL (default https://api.openai.com/v1; lets you
                         point at any OpenAI-compatible endpoint such as
                         a local vLLM or LM Studio server)
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Optional

import httpx

from .base import BrainAdapter, ChatChunk
from ._openai_compat import probe_openai_compat, stream_openai_compat


_DEFAULT_BASE = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIAdapter(BrainAdapter):
    name = "openai"
    label = "OpenAI (gpt-4o)"
    env_keys = ("OPENAI_API_KEY",)

    def __init__(
        self,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        base_url: str = _DEFAULT_BASE,
        http: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=120.0)

    @classmethod
    def try_init(cls) -> Optional["OpenAIAdapter"]:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            return None
        base = (os.environ.get("OPENAI_BASE_URL") or _DEFAULT_BASE).rstrip("/")
        if not probe_openai_compat(base, key):
            return None
        model = os.environ.get("OPENAI_MODEL") or _DEFAULT_MODEL
        return cls(api_key=key, model=model, base_url=base)

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
            self._http, self.base_url, self.api_key, self.model, messages,
            system=system, temperature=temperature,
            max_tokens=max_tokens, tools=tools,
        ):
            yield chunk
