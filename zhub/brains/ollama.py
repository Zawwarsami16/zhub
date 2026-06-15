"""OllamaAdapter — wraps a local/remote Ollama service.

Probe: GET <host>/api/version with 1s timeout.
Stream: POST <host>/api/chat with stream=true; parse newline-JSON.
Env:    OLLAMA_HOST   (default http://localhost:11434)
        OLLAMA_MODEL  (default llama3.2)
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Optional

import httpx

from .base import BrainAdapter, ChatChunk


_PROBE_TIMEOUT = 1.0
_DEFAULT_HOST = "http://localhost:11434"
_DEFAULT_MODEL = "llama3.2"


class OllamaAdapter(BrainAdapter):
    name = "ollama"
    label = "Ollama (local)"
    env_keys = ("OLLAMA_HOST",)

    def __init__(
        self,
        base_url: str = _DEFAULT_HOST,
        model: str = _DEFAULT_MODEL,
        http: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._http = http or httpx.AsyncClient(timeout=120.0)

    @classmethod
    def try_init(cls) -> Optional["OllamaAdapter"]:
        host = (os.environ.get("OLLAMA_HOST") or _DEFAULT_HOST).rstrip("/")
        model = os.environ.get("OLLAMA_MODEL") or _DEFAULT_MODEL
        try:
            r = httpx.get(f"{host}/api/version", timeout=_PROBE_TIMEOUT)
            if r.status_code != 200:
                return None
        except Exception:
            return None
        return cls(base_url=host, model=model)

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
        body = {
            "model": self.model,
            "messages": msgs,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        async with self._http.stream(
            "POST", f"{self.base_url}/api/chat", json=body,
        ) as response:
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = (data.get("message") or {}).get("content", "")
                done = bool(data.get("done"))
                yield ChatChunk(
                    delta=content,
                    done=done,
                    finish_reason=data.get("done_reason") if done else None,
                    raw=data,
                )
                if done:
                    return
