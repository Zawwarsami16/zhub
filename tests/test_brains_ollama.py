"""Tests for the Ollama brain adapter."""

import json
from typing import Iterable

import httpx
import pytest

from zhub.brains.base import ChatChunk
from zhub.brains.ollama import OllamaAdapter


# ---- helpers --------------------------------------------------------------

class _FakeStream:
    def __init__(self, lines: Iterable[str], status_code: int = 200, body: bytes = b""):
        self._lines = list(lines)
        self.status_code = status_code
        self._body = body

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


class _FakeAsyncClient:
    def __init__(self, lines: Iterable[str], status_code: int = 200, body: bytes = b""):
        self._lines = list(lines)
        self._status_code = status_code
        self._body = body
        self.last_call: dict | None = None

    def stream(self, method, url, **kw):
        self.last_call = {"method": method, "url": url, **kw}
        return _FakeStream(self._lines, status_code=self._status_code, body=self._body)

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


@pytest.mark.asyncio
async def test_stream_forwards_tools_to_request_body():
    """Ollama's /api/chat supports a top-level `tools` field. The adapter must
    forward declared tools (like its sibling adapters) — otherwise the model is
    never told they exist and can never emit a tool call."""
    tools = [{
        "type": "function",
        "function": {"name": "get_weather", "parameters": {"type": "object"}},
    }]
    fake = _FakeAsyncClient([
        json.dumps({"message": {"content": "hi"}, "done": True, "done_reason": "stop"}),
    ])
    adapter = OllamaAdapter(base_url="http://x", model="llama3.2", http=fake)
    async for _ in adapter.stream([{"role": "user", "content": "weather?"}], tools=tools):
        pass
    assert fake.last_call["json"]["tools"] == tools


@pytest.mark.asyncio
async def test_stream_omits_tools_when_none():
    """No tools passed → no `tools` key in the body (don't send an empty field)."""
    fake = _FakeAsyncClient([
        json.dumps({"message": {"content": "hi"}, "done": True, "done_reason": "stop"}),
    ])
    adapter = OllamaAdapter(base_url="http://x", model="llama3.2", http=fake)
    async for _ in adapter.stream([{"role": "user", "content": "hi"}]):
        pass
    assert "tools" not in fake.last_call["json"]


@pytest.mark.asyncio
async def test_stream_surfaces_tool_calls_as_deltas():
    """Ollama returns tool calls in `message.tool_calls` with a dict of
    arguments. The adapter must re-shape each into a hub-shaped tool_call_delta
    (string arguments) and report finish_reason='tool_calls' so the hub's
    auto-resolution fires — dropping them left an Ollama AI unable to call tools."""
    lines = [
        json.dumps({"message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": {"city": "SF"}}},
                {"function": {"name": "get_time", "arguments": {"tz": "PST"}}},
            ],
        }, "done": True, "done_reason": "stop"}),
    ]
    fake = _FakeAsyncClient(lines)
    adapter = OllamaAdapter(base_url="http://x", model="llama3.2", http=fake)
    out: list[ChatChunk] = []
    async for chunk in adapter.stream([{"role": "user", "content": "weather + time?"}]):
        out.append(chunk)

    tcds = [c.tool_call_delta for c in out if c.tool_call_delta]
    assert len(tcds) == 2
    assert tcds[0]["index"] == 0
    assert tcds[0]["type"] == "function"
    assert tcds[0]["id"]  # synthesized, non-empty
    assert tcds[0]["function"]["name"] == "get_weather"
    # arguments must be a JSON string, not a dict (the hub concatenates them)
    assert tcds[0]["function"]["arguments"] == json.dumps({"city": "SF"})
    assert tcds[1]["index"] == 1
    assert tcds[1]["function"]["name"] == "get_time"

    # the turn ended in a tool call → finish_reason normalized to tool_calls
    assert out[-1].done is True
    assert out[-1].finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_stream_raises_on_upstream_error():
    """A non-2xx response (e.g. 404 unknown model) returns a JSON error body,
    not chat events. Without a status guard the loop skips it and yields an
    empty completion silently; the adapter must raise with status + detail."""
    body = json.dumps({"error": "model 'ghost' not found"}).encode("utf-8")
    fake = _FakeAsyncClient([body.decode()], status_code=404, body=body)
    adapter = OllamaAdapter(base_url="http://x", model="ghost", http=fake)

    with pytest.raises(RuntimeError) as ei:
        async for _ in adapter.stream([{"role": "user", "content": "hi"}]):
            pass
    msg = str(ei.value)
    assert "404" in msg
    assert "model 'ghost' not found" in msg
