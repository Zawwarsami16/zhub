"""Tests for the Anthropic brain adapter."""

from typing import Iterable

import httpx
import pytest

from zhub.brains.base import ChatChunk
from zhub.brains.anthropic import AnthropicAdapter


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
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert AnthropicAdapter.try_init() is None


def test_try_init_returns_adapter_when_probe_succeeds(monkeypatch):
    class R:
        status_code = 200
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xyz")
    monkeypatch.setattr(httpx, "get",
                        lambda url, headers=None, timeout=None: R())
    adapter = AnthropicAdapter.try_init()
    assert adapter is not None
    assert adapter.name == "anthropic"
    assert adapter.api_key == "sk-ant-xyz"


@pytest.mark.asyncio
async def test_stream_parses_anthropic_sse():
    """Anthropic Messages API streams `event:` + `data:` SSE pairs.
    The relevant event types are `content_block_delta` (text deltas)
    and `message_stop` (terminal)."""
    lines = [
        'event: message_start',
        'data: {"type":"message_start","message":{"id":"msg_x"}}',
        '',
        'event: content_block_delta',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi "}}',
        '',
        'event: content_block_delta',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"there"}}',
        '',
        'event: message_delta',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
        '',
        'event: message_stop',
        'data: {"type":"message_stop"}',
        '',
    ]
    fake = _FakeAsyncClient(lines)
    adapter = AnthropicAdapter(api_key="sk-ant-xyz",
                               model="claude-sonnet-4-5",
                               http=fake)
    out = [c async for c in adapter.stream(
        [{"role": "user", "content": "hi"}], system="be brief"
    )]
    deltas = [c.delta for c in out if c.delta]
    assert "".join(deltas) == "Hi there"
    assert out[-1].done is True
    assert out[-1].finish_reason == "end_turn"

    body = fake.last_call["json"]
    headers = fake.last_call["headers"]
    assert body["model"] == "claude-sonnet-4-5"
    assert body["stream"] is True
    assert body["system"] == "be brief"
    assert body["messages"][-1] == {"role": "user", "content": "hi"}
    assert headers["x-api-key"] == "sk-ant-xyz"
    assert headers["anthropic-version"]


def test_anthropic_in_default_registry():
    from zhub.brains import REGISTRY
    names = [c.name for c in REGISTRY]
    assert "anthropic" in names


class _FakeErrorStream:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body.encode()

    async def aiter_lines(self):
        yield self._body.decode()

    async def aread(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


class _FakeErrorClient:
    def __init__(self, status_code, body):
        self._status = status_code
        self._body = body

    def stream(self, method, url, **kw):
        return _FakeErrorStream(self._status, self._body)

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_stream_raises_on_upstream_error():
    """A non-2xx body is a JSON error, not Anthropic SSE — the adapter should
    raise rather than end the stream silently with no content."""
    fake = _FakeErrorClient(529, '{"type":"error","error":{"type":"overloaded_error"}}')
    adapter = AnthropicAdapter(api_key="sk-ant-x", model="claude-sonnet-4-5", http=fake)
    with pytest.raises(RuntimeError) as ei:
        async for _ in adapter.stream([{"role": "user", "content": "hi"}]):
            pass
    msg = str(ei.value)
    assert "529" in msg
    assert "overloaded_error" in msg
