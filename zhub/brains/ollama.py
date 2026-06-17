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
        body: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        # Ollama's /api/chat natively supports function calling via a top-level
        # `tools` field. Forward them like every sibling adapter does — without
        # this the model is never told the tools exist, so it can't emit a
        # tool call and the hub's auto-resolution never fires (silent loss).
        if tools:
            body["tools"] = tools
        async with self._http.stream(
            "POST", f"{self.base_url}/api/chat", json=body,
        ) as response:
            # A non-2xx (404 unknown model, 400 bad request, 5xx) returns a
            # single JSON error object like {"error": "model ... not found"},
            # not newline-delimited chat events — the loop below would find no
            # message.content, never see done=True, and the generator would end
            # yielding nothing (or one empty delta): a silent empty completion
            # with no diagnostic. Raise so the publisher surfaces a real error,
            # matching the openai-compat / anthropic / cohere adapters. getattr
            # default keeps test doubles working; real httpx responses always
            # have status_code.
            status = getattr(response, "status_code", 200)
            if status >= 400:
                try:
                    detail = (await response.aread()).decode("utf-8", "replace").strip()
                except Exception:
                    detail = ""
                snippet = f": {detail[:300]}" if detail else ""
                raise RuntimeError(f"upstream returned HTTP {status}{snippet}")
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = data.get("message") or {}
                content = message.get("content") or ""
                done = bool(data.get("done"))
                # Ollama returns tool calls in `message.tool_calls` as complete
                # objects (name + a dict of arguments), not as OpenAI-style
                # incremental deltas. Re-shape each into a tool_call_delta the
                # hub understands: index, synthetic id, function name, and
                # arguments serialized to a JSON string (the hub concatenates
                # argument fragments, so it must be a str). Dropping these meant
                # an Ollama-backed AI could never resolve a capability.
                tool_calls = message.get("tool_calls") or []
                for i, tc in enumerate(tool_calls):
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False) if args else ""
                    yield ChatChunk(
                        delta="",
                        done=False,
                        tool_call_delta={
                            "index": i,
                            "id": tc.get("id") or f"call_{i}",
                            "type": "function",
                            "function": {"name": fn.get("name", ""), "arguments": args},
                        },
                        raw=data,
                    )
                if done:
                    # Ollama reports done_reason="stop" even when the turn ended
                    # in a tool call; the hub keys auto-resolution off
                    # finish_reason == "tool_calls", so report that when calls
                    # were present this turn.
                    finish = "tool_calls" if tool_calls else (data.get("done_reason") or "stop")
                    yield ChatChunk(delta=content, done=True, finish_reason=finish, raw=data)
                    return
                yield ChatChunk(delta=content, done=False, raw=data)
