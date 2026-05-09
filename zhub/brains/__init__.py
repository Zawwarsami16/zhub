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

from typing import Optional

from .base import BrainAdapter, ChatChunk
from .ollama import OllamaAdapter
from .groq import GroqAdapter
from .openai import OpenAIAdapter
from .cerebras import CerebrasAdapter


REGISTRY: list[type[BrainAdapter]] = [
    OllamaAdapter,
    GroqAdapter,
    OpenAIAdapter,
    CerebrasAdapter,
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
    "detect", "list_available",
    "OllamaAdapter", "GroqAdapter", "OpenAIAdapter", "CerebrasAdapter",
]
