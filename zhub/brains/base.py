"""Abstract interface for brain adapters.

A BrainAdapter wraps one upstream LLM (Ollama, Groq, OpenAI, Cerebras,
or a custom inference server) so that any zhub publisher can use it as
a streaming chat source. Adapters are async-iterator first — they yield
ChatChunk objects with incremental deltas, then a final chunk with
done=True.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional


@dataclass
class ChatChunk:
    """One incremental piece of a streaming chat response.

    delta: incremental text emitted in this chunk (may be empty when the
        chunk only signals end-of-stream).
    done: True on the final chunk.
    finish_reason: standard reason string ("stop", "length", etc.) on the
        final chunk; None elsewhere.
    raw: the underlying API's chunk dict, kept for debugging.
    """

    delta: str = ""
    done: bool = False
    finish_reason: Optional[str] = None
    raw: Optional[dict] = None


class BrainAdapter(abc.ABC):
    """Abstract brain — one concrete subclass per upstream LLM provider."""

    name: str = ""    # short id, e.g. "ollama"
    label: str = ""   # human-readable, e.g. "Ollama (local)"

    @classmethod
    @abc.abstractmethod
    def try_init(cls) -> Optional["BrainAdapter"]:
        """Probe the upstream service. Return an instance if credentials
        and reachability check out, None otherwise. Probes time out fast
        (<1s) so detection stays snappy. Errors are silent — try_init
        never raises; it returns None on any failure."""

    @abc.abstractmethod
    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> AsyncIterator[ChatChunk]:
        """Stream the model's reply. Final chunk has done=True. Adapters
        ignore `tools` if their upstream doesn't natively support function
        calling — zhub's hub-side resolution doesn't need adapter help."""
