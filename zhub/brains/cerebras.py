"""CerebrasAdapter — wraps Cerebras Cloud's chat completions API.

Cerebras serves OpenAI-shape chat completions at https://api.cerebras.ai/v1.

Probe: GET /v1/models, 1.5s timeout.
Env:   CEREBRAS_API_KEY  (required)
       CEREBRAS_MODEL    (default llama-3.3-70b)
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Optional

import httpx

from .base import BrainAdapter, ChatChunk
from ._openai_compat import probe_openai_compat, stream_openai_compat


_BASE_URL = "https://api.cerebras.ai/v1"
_DEFAULT_MODEL = "llama-3.3-70b"


class CerebrasAdapter(BrainAdapter):
    name = "cerebras"
    label = "Cerebras Llama 3.1 405B"

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
    def try_init(cls) -> Optional["CerebrasAdapter"]:
        key = os.environ.get("CEREBRAS_API_KEY")
        if not key or not probe_openai_compat(_BASE_URL, key):
            return None
        model = os.environ.get("CEREBRAS_MODEL") or _DEFAULT_MODEL
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
