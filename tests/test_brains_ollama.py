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
