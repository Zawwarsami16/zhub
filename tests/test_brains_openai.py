"""Tests for the OpenAI brain adapter."""

from typing import Iterable

import httpx
import pytest

from zhub.brains.base import ChatChunk
from zhub.brains.openai import OpenAIAdapter


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
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert OpenAIAdapter.try_init() is None


def test_try_init_none_when_probe_fails(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(httpx, "get",
                        lambda url, headers=None, timeout=None: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert OpenAIAdapter.try_init() is None


def test_try_init_returns_adapter_when_probe_succeeds(monkeypatch):
    class R:
        status_code = 200
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: R())
    adapter = OpenAIAdapter.try_init()
    assert adapter is not None
    assert adapter.name == "openai"
    assert adapter.api_key == "sk-test"


@pytest.mark.asyncio
async def test_stream_parses_openai_sse():
    lines = [
        'data: {"choices":[{"delta":{"role":"assistant","content":"He"}}]}',
        'data: {"choices":[{"delta":{"content":"y"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ]
    fake = _FakeAsyncClient(lines)
    adapter = OpenAIAdapter(api_key="sk-test", model="gpt-4o-mini", http=fake)
    out = [c async for c in adapter.stream(
        [{"role": "user", "content": "hi"}], system="be brief"
    )]
    deltas = [c.delta for c in out if c.delta]
    assert "".join(deltas) == "Hey"
    assert out[-1].done is True
    assert out[-1].finish_reason == "stop"

    headers = fake.last_call["headers"]
    body = fake.last_call["json"]
    assert headers["Authorization"] == "Bearer sk-test"
    assert body["model"] == "gpt-4o-mini"
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
