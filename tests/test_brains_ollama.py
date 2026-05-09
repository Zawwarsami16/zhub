"""Tests for the Ollama brain adapter."""

import json
from typing import Iterable

import httpx
import pytest

from zhub.brains.base import ChatChunk
from zhub.brains.ollama import OllamaAdapter


# ---- helpers --------------------------------------------------------------

class _FakeStream:
    def __init__(self, lines: Iterable[str]):
        self._lines = list(lines)

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


class _FakeAsyncClient:
    def __init__(self, lines: Iterable[str]):
        self._lines = list(lines)
        self.last_call: dict | None = None

    def stream(self, method, url, **kw):
        self.last_call = {"method": method, "url": url, **kw}
        return _FakeStream(self._lines)

    async def aclose(self):
        pass


# ---- try_init -------------------------------------------------------------

def test_try_init_returns_none_when_probe_fails(monkeypatch):
    def fake_get(url, timeout=None):
        raise httpx.ConnectError("nope")
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert OllamaAdapter.try_init() is None


def test_try_init_returns_none_when_probe_non_200(monkeypatch):
    class R:
        status_code = 500
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: R())
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert OllamaAdapter.try_init() is None


def test_try_init_returns_adapter_when_probe_succeeds(monkeypatch):
    class R:
        status_code = 200
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: R())
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2")
    adapter = OllamaAdapter.try_init()
    assert adapter is not None
    assert adapter.name == "ollama"
    assert adapter.base_url == "http://localhost:11434"
    assert adapter.model == "llama3.2"


# ---- stream ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_parses_ollama_line_json():
    """Ollama streams newline-delimited JSON with message.content deltas."""
    lines = [
        json.dumps({"message": {"content": "Hello"}, "done": False}),
        json.dumps({"message": {"content": " world"}, "done": False}),
        json.dumps({"message": {"content": "!"}, "done": True, "done_reason": "stop"}),
    ]
    fake = _FakeAsyncClient(lines)
    adapter = OllamaAdapter(base_url="http://x", model="llama3.2", http=fake)

    out: list[ChatChunk] = []
    async for chunk in adapter.stream(
        [{"role": "user", "content": "hi"}], system="you are short"
    ):
        out.append(chunk)

    assert [c.delta for c in out] == ["Hello", " world", "!"]
    assert out[-1].done is True
    assert out[-1].finish_reason == "stop"
    body = fake.last_call["json"]
    assert body["model"] == "llama3.2"
    assert body["stream"] is True
    assert body["messages"][0] == {"role": "system", "content": "you are short"}
    assert body["messages"][-1] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_stream_skips_empty_and_malformed_lines():
    lines = [
        "",
        "not json at all",
        json.dumps({"message": {"content": "ok"}, "done": True, "done_reason": "stop"}),
    ]
    fake = _FakeAsyncClient(lines)
    adapter = OllamaAdapter(base_url="http://x", model="llama3.2", http=fake)
    out = [c async for c in adapter.stream([{"role": "user", "content": "hi"}])]
    assert [c.delta for c in out] == ["ok"]
    assert out[-1].done is True
