"""Tests for the brains REGISTRY, detect(), and list_available()."""

from typing import Optional

from zhub.brains import REGISTRY, detect, list_available
from zhub.brains.base import BrainAdapter, ChatChunk


class _AlwaysAvailable(BrainAdapter):
    name = "always"
    label = "Always-available test adapter"

    @classmethod
    def try_init(cls) -> Optional["BrainAdapter"]:
        return cls()

    async def stream(self, messages, *, system=None, temperature=0.7,
                     max_tokens=2048, tools=None):
        yield ChatChunk(delta="ok", done=True)


class _NeverAvailable(BrainAdapter):
    name = "never"
    label = "Never-available test adapter"

    @classmethod
    def try_init(cls) -> Optional["BrainAdapter"]:
        return None

    async def stream(self, messages, *, system=None, temperature=0.7,
                     max_tokens=2048, tools=None):
        yield ChatChunk(delta="", done=True)


def test_registry_holds_classes_in_priority_order():
    """REGISTRY is a list of adapter CLASSES in detection priority order.
    The four shipped adapters land in this order: Ollama, Groq, OpenAI,
    Cerebras."""
    names = [cls.name for cls in REGISTRY]
    assert names == ["ollama", "groq", "openai", "cerebras"]


def test_detect_returns_first_available(monkeypatch):
    """detect() walks REGISTRY and returns the first try_init() that
    yields a non-None instance."""
    monkeypatch.setattr("zhub.brains.REGISTRY", [_NeverAvailable, _AlwaysAvailable])
    adapter = detect()
    assert adapter is not None
    assert adapter.name == "always"


def test_detect_returns_none_when_all_unavailable(monkeypatch):
    monkeypatch.setattr("zhub.brains.REGISTRY", [_NeverAvailable])
    assert detect() is None


def test_list_available_returns_only_initialized(monkeypatch):
    monkeypatch.setattr("zhub.brains.REGISTRY",
                        [_NeverAvailable, _AlwaysAvailable, _NeverAvailable])
    avail = list_available()
    assert [a.name for a in avail] == ["always"]


def test_list_available_empty_when_none(monkeypatch):
    monkeypatch.setattr("zhub.brains.REGISTRY", [_NeverAvailable])
    assert list_available() == []
