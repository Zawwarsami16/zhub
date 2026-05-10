"""GroqAdapter — wraps Groq's OpenAI-compatible API.

Probe: GET https://api.groq.com/openai/v1/models with bearer key, 1.5s timeout.
Stream: POST /openai/v1/chat/completions with stream=true, parses SSE.
Env:    GROQ_API_KEY  (required)
        GROQ_MODEL    (default llama-3.3-70b-versatile)
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Optional

import httpx

from .base import BrainAdapter, ChatChunk


_PROBE_TIMEOUT = 1.5
_BASE_URL = "https://api.groq.com/openai/v1"
_DEFAULT_MODEL = "llama-3.3-70b-versatile"


class GroqAdapter(BrainAdapter):
    name = "groq"
    label = "Groq Llama 3.3 70B"

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
        if not key:
            return None
        try:
            r = httpx.get(
                f"{_BASE_URL}/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=_PROBE_TIMEOUT,
            )
            if r.status_code != 200:
                return None
        except Exception:
            return None
        model = os.environ.get("GROQ_MODEL", _DEFAULT_MODEL)
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
            "POST", f"{_BASE_URL}/chat/completions",
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
                delta_obj = choice.get("delta") or {}
                content = delta_obj.get("content") or ""
                finish = choice.get("finish_reason")
                tool_call_deltas = delta_obj.get("tool_calls") or []
                # OpenAI-shape: each delta.tool_calls element is one
                # tool-call's incremental update. Surface each as its own
                # ChatChunk so downstream sees them in arrival order.
                for tcd in tool_call_deltas:
                    yield ChatChunk(
                        delta="",
                        done=False,
                        tool_call_delta=tcd,
                        raw=data,
                    )
                if content:
                    yield ChatChunk(
                        delta=content,
                        done=False,
                        raw=data,
                    )
                if finish:
                    yield ChatChunk(
                        delta="",
                        done=True,
                        finish_reason=finish,
                        raw=data,
                    )
                    return
