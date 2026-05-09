"""OpenAIAdapter — wraps OpenAI's chat completions API.

Probe: GET https://api.openai.com/v1/models, 1.5s timeout.
Stream: POST /v1/chat/completions with stream=true; parses standard
        OpenAI SSE.
Env:    OPENAI_API_KEY  (required)
        OPENAI_MODEL    (default gpt-4o-mini)
        OPENAI_BASE_URL (default https://api.openai.com/v1; lets you
                         point at any OpenAI-compatible endpoint such as
                         a local vLLM or LM Studio server)
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Optional

import httpx

from .base import BrainAdapter, ChatChunk


_PROBE_TIMEOUT = 1.5
_DEFAULT_BASE = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIAdapter(BrainAdapter):
    name = "openai"
    label = "OpenAI (gpt-4o)"

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
        base = os.environ.get("OPENAI_BASE_URL", _DEFAULT_BASE).rstrip("/")
        try:
            r = httpx.get(
                f"{base}/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=_PROBE_TIMEOUT,
            )
            if r.status_code != 200:
                return None
        except Exception:
            return None
        model = os.environ.get("OPENAI_MODEL", _DEFAULT_MODEL)
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
            "POST", f"{self.base_url}/chat/completions",
            json=body, headers=headers,
        ) as response:
            async for line in response.aiter_lines():
                line = line.rstrip("\r")
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    yield ChatChunk(delta="", done=True, finish_reason="stop")
                    return
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = (choice.get("delta") or {}).get("content") or ""
                finish = choice.get("finish_reason")
                yield ChatChunk(
                    delta=delta,
                    done=bool(finish),
                    finish_reason=finish,
                    raw=data,
                )
                if finish:
                    return
