"""Unit tests for the BrainAdapter ABC and ChatChunk dataclass."""

import inspect
import pytest

from zhub.brains.base import BrainAdapter, ChatChunk


def test_chatchunk_has_expected_fields_and_defaults():
    chunk = ChatChunk()
    assert chunk.delta == ""
    assert chunk.done is False
    assert chunk.finish_reason is None
    assert chunk.raw is None


def test_chatchunk_accepts_all_fields():
    chunk = ChatChunk(delta="hi", done=True, finish_reason="stop", raw={"x": 1})
    assert chunk.delta == "hi"
    assert chunk.done is True
    assert chunk.finish_reason == "stop"
    assert chunk.raw == {"x": 1}


def test_brainadapter_cannot_be_instantiated():
    with pytest.raises(TypeError):
        BrainAdapter()  # type: ignore[abstract]


def test_brainadapter_subclass_must_implement_try_init_and_stream():
    """A subclass that omits both abstract methods can't be instantiated."""
    class Incomplete(BrainAdapter):
        name = "x"
        label = "x"

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_brainadapter_concrete_subclass_can_instantiate():
    class Concrete(BrainAdapter):
        name = "concrete"
        label = "Concrete test adapter"

        @classmethod
        def try_init(cls):
            return cls()

        async def stream(self, messages, *, system=None, temperature=0.7,
                         max_tokens=2048, tools=None):
            yield ChatChunk(delta="ok", done=True)

    assert Concrete().name == "concrete"
    assert inspect.isasyncgenfunction(Concrete.stream)
