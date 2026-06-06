"""Shared streaming + probe logic for OpenAI-compatible brain adapters.

OpenAI's chat completions wire format is the de facto standard — Groq,
OpenAI itself, Cerebras, Together, Mistral, and many self-hosted servers
(vLLM, LM Studio, llama.cpp) all speak it identically. This module factors
the common parts so each provider's adapter is a thin shell.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Optional

import httpx

from .base import BrainAdapter, ChatChunk


_PROBE_TIMEOUT = 1.5


def probe_openai_compat(base_url: str, api_key: str) -> bool:
    """Cheap reachability probe — GET /models with bearer key."""
    try:
        r = httpx.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_PROBE_TIMEOUT,
        )
        return r.status_code == 200
    except Exception:
        return False


async def stream_openai_compat(
    http: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    system: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    tools: Optional[list[dict[str, Any]]] = None,
    extra_headers: Optional[dict[str, str]] = None,
) -> AsyncIterator[ChatChunk]:
    """Stream chat completions from any OpenAI-compatible endpoint, parsing
    the SSE shape `data: {"choices":[{"delta":{...}, "finish_reason":...}]}`
    terminated by `data: [DONE]`. Surfaces text deltas, tool_call deltas,
    and a final done chunk with finish_reason."""
    msgs: list[dict[str, Any]] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)
    body: dict[str, Any] = {
        "model": model,
        "messages": msgs,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        body["tools"] = tools
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    async with http.stream(
        "POST", f"{base_url.rstrip('/')}/chat/completions",
        json=body, headers=headers,
    ) as response:
        # An upstream error (429 rate limit, 401 bad key, 5xx) comes back as a
        # JSON body that isn't SSE, so the line loop below would skip every
        # line and the generator would end yielding nothing — a silent empty
        # completion. Catch it here and raise so the publisher surfaces a real
        # error to the caller instead. getattr default keeps test doubles that
        # don't set status_code working; real httpx responses always have it.
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
            for tcd in delta_obj.get("tool_calls") or []:
                yield ChatChunk(delta="", done=False,
                                tool_call_delta=tcd, raw=data)
            if content:
                yield ChatChunk(delta=content, done=False, raw=data)
            if finish:
                yield ChatChunk(delta="", done=True,
                                finish_reason=finish, raw=data)
                return
