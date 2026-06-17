"""Phase 11.0 — tests for Together, Mistral, Cohere brain adapters."""

import json
from typing import Iterable

import httpx
import pytest

from zhub.brains.together import TogetherAdapter
from zhub.brains.mistral import MistralAdapter
from zhub.brains.cohere import CohereAdapter


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


# ---- Together (OpenAI-compat) ------------------------------------------

def test_together_try_init_none_without_key(monkeypatch):
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    assert TogetherAdapter.try_init() is None


def test_together_try_init_returns_adapter(monkeypatch):
    class R:
        status_code = 200
    monkeypatch.setenv("TOGETHER_API_KEY", "tg_test")
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: R())
    a = TogetherAdapter.try_init()
    assert a is not None and a.name == "together"
    assert a.api_key == "tg_test"


@pytest.mark.asyncio
async def test_together_stream_uses_openai_compat():
    lines = [
        'data: {"choices":[{"delta":{"content":"hi"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ]
    fake = _FakeAsyncClient(lines)
    a = TogetherAdapter(api_key="tg_test", model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
                         http=fake)
    out = [c async for c in a.stream([{"role": "user", "content": "x"}])]
    assert any(c.delta == "hi" for c in out)
    assert out[-1].done is True
    assert fake.last_call["headers"]["Authorization"] == "Bearer tg_test"


# ---- Mistral (OpenAI-compat) -------------------------------------------

def test_mistral_try_init_none_without_key(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    assert MistralAdapter.try_init() is None


def test_mistral_try_init_returns_adapter(monkeypatch):
    class R:
        status_code = 200
    monkeypatch.setenv("MISTRAL_API_KEY", "ms_test")
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: R())
    a = MistralAdapter.try_init()
    assert a is not None and a.name == "mistral"


@pytest.mark.asyncio
async def test_mistral_stream_uses_openai_compat():
    lines = [
        'data: {"choices":[{"delta":{"content":"bonjour"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ]
    fake = _FakeAsyncClient(lines)
    a = MistralAdapter(api_key="ms_test", model="mistral-large-latest", http=fake)
    out = [c async for c in a.stream([{"role": "user", "content": "x"}])]
    assert any(c.delta == "bonjour" for c in out)
    assert out[-1].done is True


# ---- Cohere (custom v2 shape) ------------------------------------------

def test_cohere_try_init_none_without_key(monkeypatch):
    monkeypatch.delenv("COHERE_API_KEY", raising=False)
    assert CohereAdapter.try_init() is None


def test_cohere_try_init_returns_adapter(monkeypatch):
    class R:
        status_code = 200
    monkeypatch.setenv("COHERE_API_KEY", "co_test")
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: R())
    a = CohereAdapter.try_init()
    assert a is not None and a.name == "cohere"


@pytest.mark.asyncio
async def test_cohere_stream_parses_v2_event_shape():
    """Cohere v2 streams newline-JSON with type discriminator."""
    lines = [
        json.dumps({"type": "message-start", "id": "m_x"}),
        json.dumps({"type": "content-delta",
                    "delta": {"message": {"content": {"text": "Howdy "}}}}),
        json.dumps({"type": "content-delta",
                    "delta": {"message": {"content": {"text": "partner"}}}}),
        json.dumps({"type": "message-end",
                    "delta": {"finish_reason": "complete"}}),
    ]
    fake = _FakeAsyncClient(lines)
    a = CohereAdapter(api_key="co_test", model="command-r-plus-08-2024",
                      http=fake)
    out = [c async for c in a.stream([{"role": "user", "content": "x"}])]
    deltas = [c.delta for c in out if c.delta]
    assert "".join(deltas) == "Howdy partner"
    assert out[-1].done is True
    # `complete` normalized to `stop`
    assert out[-1].finish_reason == "stop"
    body = fake.last_call["json"]
    assert body["model"] == "command-r-plus-08-2024"
    assert body["messages"][-1] == {"role": "user", "content": "x"}
    headers = fake.last_call["headers"]
    assert headers["Authorization"] == "Bearer co_test"


@pytest.mark.asyncio
async def test_cohere_stream_surfaces_tool_calls_as_deltas():
    """Cohere v2 streams tool calls as tool-call-start (id + name) followed by
    tool-call-delta argument fragments, ending finish_reason=TOOL_CALL. The
    adapter must surface these as hub-shaped tool_call_deltas (string-concat
    arguments) and map the finish to "tool_calls" — otherwise a Cohere-backed
    AI can never resolve a capability (sibling of the ollama/anthropic gaps)."""
    lines = [
        json.dumps({"type": "message-start", "id": "m"}),
        json.dumps({"type": "tool-call-start", "index": 0,
                    "delta": {"message": {"tool_calls": {
                        "id": "tc_1", "type": "function",
                        "function": {"name": "get_weather", "arguments": ""}}}}}),
        json.dumps({"type": "tool-call-delta", "index": 0,
                    "delta": {"message": {"tool_calls": {
                        "function": {"arguments": "{\"city\""}}}}}),
        json.dumps({"type": "tool-call-delta", "index": 0,
                    "delta": {"message": {"tool_calls": {
                        "function": {"arguments": ":\"Paris\"}"}}}}}),
        json.dumps({"type": "message-end",
                    "delta": {"finish_reason": "TOOL_CALL"}}),
    ]
    fake = _FakeAsyncClient(lines)
    a = CohereAdapter(api_key="co_test", model="command-r-plus-08-2024",
                      http=fake)
    tools = [{"type": "function", "function": {"name": "get_weather"}}]
    out = [c async for c in a.stream([{"role": "user", "content": "x"}],
                                     tools=tools)]
    # tools forwarded into the request body
    assert fake.last_call["json"]["tools"] == tools
    tcds = [c.tool_call_delta for c in out if c.tool_call_delta]
    assert len(tcds) == 3
    # opener carries id + name under index 0
    assert tcds[0]["index"] == 0
    assert tcds[0]["id"] == "tc_1"
    assert tcds[0]["type"] == "function"
    assert tcds[0]["function"]["name"] == "get_weather"
    # argument fragments concatenate to the full JSON, all under index 0
    args = "".join(t["function"]["arguments"] for t in tcds)
    assert args == "{\"city\":\"Paris\"}"
    assert all(t["index"] == 0 for t in tcds)
    # tool-call turn finish maps to the hub's "tool_calls"
    assert out[-1].done is True
    assert out[-1].finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_cohere_stream_omits_tools_when_none():
    """No tools passed → no `tools` key in the request body."""
    lines = [
        json.dumps({"type": "content-delta",
                    "delta": {"message": {"content": {"text": "hi"}}}}),
        json.dumps({"type": "message-end",
                    "delta": {"finish_reason": "complete"}}),
    ]
    fake = _FakeAsyncClient(lines)
    a = CohereAdapter(api_key="co_test", model="command-r-plus-08-2024",
                      http=fake)
    _ = [c async for c in a.stream([{"role": "user", "content": "x"}])]
    assert "tools" not in fake.last_call["json"]


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
async def test_cohere_stream_raises_on_upstream_error():
    """A non-2xx body is a JSON error, not Cohere's newline-delimited events —
    the adapter must raise rather than end the stream silently with no content
    (matching the anthropic / openai-compat adapters)."""
    fake = _FakeErrorClient(429, '{"message":"rate limit exceeded"}')
    a = CohereAdapter(api_key="co_test", model="command-r-plus-08-2024", http=fake)
    with pytest.raises(RuntimeError) as ei:
        async for _ in a.stream([{"role": "user", "content": "hi"}]):
            pass
    msg = str(ei.value)
    assert "429" in msg
    assert "rate limit exceeded" in msg
