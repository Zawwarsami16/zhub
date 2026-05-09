"""Tests for the Cerebras brain adapter."""

from typing import Iterable

import httpx
import pytest

from zhub.brains.base import ChatChunk
from zhub.brains.cerebras import CerebrasAdapter


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
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    assert CerebrasAdapter.try_init() is None


def test_try_init_returns_adapter_when_probe_succeeds(monkeypatch):
    class R:
        status_code = 200
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk-test")
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: R())
    adapter = CerebrasAdapter.try_init()
    assert adapter is not None
    assert adapter.name == "cerebras"
    assert adapter.api_key == "csk-test"


@pytest.mark.asyncio
async def test_stream_parses_openai_sse():
    lines = [
        'data: {"choices":[{"delta":{"role":"assistant","content":"Lo"}}]}',
        'data: {"choices":[{"delta":{"content":"l"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ]
    fake = _FakeAsyncClient(lines)
    adapter = CerebrasAdapter(api_key="csk-test",
                              model="llama-3.3-70b", http=fake)
    out = [c async for c in adapter.stream(
        [{"role": "user", "content": "hi"}]
    )]
    deltas = [c.delta for c in out if c.delta]
    assert "".join(deltas) == "Lol"
    assert out[-1].done is True
    headers = fake.last_call["headers"]
    assert headers["Authorization"] == "Bearer csk-test"
