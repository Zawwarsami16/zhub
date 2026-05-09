"""Tests for the Groq brain adapter (OpenAI-compatible SSE format)."""

from typing import Iterable

import httpx
import pytest

from zhub.brains.base import ChatChunk
from zhub.brains.groq import GroqAdapter


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


def test_try_init_none_without_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert GroqAdapter.try_init() is None


def test_try_init_none_when_probe_fails(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr(httpx, "get",
                        lambda url, headers=None, timeout=None: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert GroqAdapter.try_init() is None


def test_try_init_returns_adapter_when_probe_succeeds(monkeypatch):
    class R:
        status_code = 200
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: R())
    adapter = GroqAdapter.try_init()
    assert adapter is not None
    assert adapter.name == "groq"
    assert adapter.api_key == "gsk_test"


@pytest.mark.asyncio
async def test_stream_parses_openai_sse():
    """Groq emits OpenAI-shape SSE."""
    lines = [
        'data: {"choices":[{"delta":{"role":"assistant","content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        'data: {"choices":[{"delta":{"content":"!"},"finish_reason":"stop"}]}',
        'data: [DONE]',
        '',
    ]
    fake = _FakeAsyncClient(lines)
    adapter = GroqAdapter(api_key="gsk_test", model="llama-3.3-70b-versatile",
                          http=fake)
    out: list[ChatChunk] = []
    async for chunk in adapter.stream(
        [{"role": "user", "content": "hi"}], system="you are short"
    ):
        out.append(chunk)
    deltas = [c.delta for c in out if c.delta]
    assert "".join(deltas) == "Hello!"
    assert out[-1].done is True
    assert out[-1].finish_reason == "stop"

    body = fake.last_call["json"]
    headers = fake.last_call["headers"]
    assert body["model"] == "llama-3.3-70b-versatile"
    assert body["stream"] is True
    assert body["messages"][0] == {"role": "system", "content": "you are short"}
    assert body["messages"][-1] == {"role": "user", "content": "hi"}
    assert headers["Authorization"] == "Bearer gsk_test"


@pytest.mark.asyncio
async def test_stream_ignores_lines_without_data_prefix():
    lines = [
        ': comment',
        'event: heartbeat',
        'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ]
    fake = _FakeAsyncClient(lines)
    adapter = GroqAdapter(api_key="k", model="llama-3.3-70b-versatile", http=fake)
    out = [c async for c in adapter.stream([{"role": "user", "content": "hi"}])]
    assert [c.delta for c in out if c.delta] == ["ok"]
    assert out[-1].done is True
