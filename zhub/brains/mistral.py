"""MistralAdapter — wraps Mistral AI's chat completions API.

Mistral hosts their own line of open models (mistral-small/medium/large,
mixtral, pixtral, codestral) at https://api.mistral.ai/v1, OpenAI-compat.

Probe: GET /v1/models, 1.5s timeout.
Env:   MISTRAL_API_KEY  (required)
       MISTRAL_MODEL    (default mistral-large-latest)
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Optional

import httpx

from .base import BrainAdapter, ChatChunk
from ._openai_compat import probe_openai_compat, stream_openai_compat


_BASE_URL = "https://api.mistral.ai/v1"
_DEFAULT_MODEL = "mistral-large-latest"


class MistralAdapter(BrainAdapter):
    name = "mistral"
    label = "Mistral Large"
    env_keys = ("MISTRAL_API_KEY",)

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
    def try_init(cls) -> Optional["MistralAdapter"]:
        key = os.environ.get("MISTRAL_API_KEY")
        if not key or not probe_openai_compat(_BASE_URL, key):
            return None
        model = os.environ.get("MISTRAL_MODEL") or _DEFAULT_MODEL
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
