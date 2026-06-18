"""Brain adapters — wraps any LLM API as a streaming source for zhub.publish.

Public surface:
    BrainAdapter, ChatChunk      — interface + chunk type
    REGISTRY                     — list of adapter classes in priority order
    detect()                     — first available adapter, or None
    list_available()             — every available adapter

Detection probes are cheap (1s timeout) and silent on failure. Importing
this module does NOT touch the network — only detect() / list_available()
do.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from .base import BrainAdapter, ChatChunk
from .ollama import OllamaAdapter
from .groq import GroqAdapter
from .openai import OpenAIAdapter
from .cerebras import CerebrasAdapter
from .anthropic import AnthropicAdapter
from .together import TogetherAdapter
from .mistral import MistralAdapter
from .cohere import CohereAdapter


REGISTRY: list[type[BrainAdapter]] = [
    OllamaAdapter,
    GroqAdapter,
    OpenAIAdapter,
    CerebrasAdapter,
    AnthropicAdapter,
    TogetherAdapter,
    MistralAdapter,
    CohereAdapter,
]


def detect() -> Optional[BrainAdapter]:
    """Return the first available adapter in priority order, or None."""
    # Re-read the (possibly monkeypatched) module-level REGISTRY so tests
    # can swap in fakes without touching this import.
    import zhub.brains as _self
    for cls in _self.REGISTRY:
        adapter = cls.try_init()
        if adapter is not None:
            return adapter
    return None


async def stream_for_publish(
    brain: BrainAdapter,
    messages: list[dict[str, Any]],
    *,
    system: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    tools: Optional[list[dict[str, Any]]] = None,
) -> AsyncIterator[ChatChunk]:
    """Stream a brain's reply as chunks suitable to yield from a zhub
    ``chat_handler``.

    Yields the *whole* ChatChunk whenever it carries a text delta, a
    tool_call_delta, OR a finish_reason — not just ``chunk.delta``. A
    tool-call turn produces chunks whose ``delta`` is empty but whose
    ``tool_call_delta`` / ``finish_reason="tool_calls"`` carry the call;
    yielding only ``chunk.delta`` (the obvious-looking shortcut) silently
    drops every function call the brain emits, so the hub — which keys
    tool-call auto-resolution off accumulated tool_call deltas + a
    ``finish_reason == "tool_calls"`` terminator — never sees them. The
    publisher serializers (`_serialize_stream_chunk` / `_chunk_fields`)
    already understand ChatChunk objects, so handing them the chunk
    verbatim preserves the full shape on both the streaming and
    non-streaming paths.
    """
    async for chunk in brain.stream(
        messages, system=system, temperature=temperature,
        max_tokens=max_tokens, tools=tools,
    ):
        if chunk.delta or chunk.tool_call_delta or chunk.finish_reason:
            yield chunk


def list_available() -> list[BrainAdapter]:
    import zhub.brains as _self
    out: list[BrainAdapter] = []
    for cls in _self.REGISTRY:
        adapter = cls.try_init()
        if adapter is not None:
            out.append(adapter)
    return out


__all__ = [
    "BrainAdapter", "ChatChunk", "REGISTRY",
    "detect", "list_available", "stream_for_publish",
    "OllamaAdapter", "GroqAdapter", "OpenAIAdapter",
    "CerebrasAdapter", "AnthropicAdapter",
    "TogetherAdapter", "MistralAdapter", "CohereAdapter",
]
