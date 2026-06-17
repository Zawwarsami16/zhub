"""Non-streaming accumulation of tool calls in _handle_chat.

When a chat_handler is a generator (the shape every brain adapter uses) and the
caller did NOT request streaming, _handle_chat folds the yielded chunks into a
single chat-response. It historically collected only text deltas and hardcoded
finish_reason="stop", so a generator that emitted tool_call deltas lost every
tool call and reported the wrong finish reason. The hub's non-streaming tool
resolution gates on response["tool_calls"] (server.py), so a dropped tool_calls
list means the capability is never auto-resolved — a silent loss that defeats
the adapters' tool forwarding on every non-streaming request.

These tests pin the accumulator to carry tool_calls + the real finish_reason.
"""

import json

import pytest

from zhub.client import _handle_chat
from zhub.protocol import Envelope


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


class _Pub:
    def __init__(self, handler) -> None:
        self.chat_handler = handler


def _response(ws: _FakeWS) -> dict:
    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "chat-response"
    return ws.sent[0]["payload"]


@pytest.mark.asyncio
async def test_async_gen_nonstreaming_accumulates_tool_calls():
    async def handler(messages, options):
        yield {"tool_call_delta": {"index": 0, "id": "call_1",
                                   "function": {"name": "get_weather",
                                                "arguments": '{"city":'}}}
        yield {"tool_call_delta": {"index": 0,
                                   "function": {"arguments": '"Paris"}'}}}
        yield {"done": True, "finish_reason": "tool_calls"}

    ws = _FakeWS()
    env = Envelope(type="chat-request", payload={"messages": []})
    await _handle_chat(_Pub(handler), ws, env)

    p = _response(ws)
    assert p["finish_reason"] == "tool_calls"
    assert p["tool_calls"] == [{
        "id": "call_1",
        "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
    }]


@pytest.mark.asyncio
async def test_async_gen_nonstreaming_text_and_tool_calls_both_survive():
    async def handler(messages, options):
        yield {"delta": "let me check "}
        yield {"tool_call_delta": {"index": 0, "id": "c1",
                                   "function": {"name": "lookup",
                                                "arguments": "{}"}}}
        yield {"delta": "the db"}
        yield {"done": True, "finish_reason": "tool_calls"}

    ws = _FakeWS()
    env = Envelope(type="chat-request", payload={"messages": []})
    await _handle_chat(_Pub(handler), ws, env)

    p = _response(ws)
    assert p["text"] == "let me check the db"
    assert p["tool_calls"][0]["function"]["name"] == "lookup"
    assert p["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_async_gen_nonstreaming_multiple_tool_calls_sorted_by_index():
    async def handler(messages, options):
        # Out-of-order indices; the response must order them by index.
        yield {"tool_call_delta": {"index": 1, "id": "b",
                                   "function": {"name": "second",
                                                "arguments": "{}"}}}
        yield {"tool_call_delta": {"index": 0, "id": "a",
                                   "function": {"name": "first",
                                                "arguments": "{}"}}}
        yield {"done": True, "finish_reason": "tool_calls"}

    ws = _FakeWS()
    env = Envelope(type="chat-request", payload={"messages": []})
    await _handle_chat(_Pub(handler), ws, env)

    p = _response(ws)
    assert [tc["id"] for tc in p["tool_calls"]] == ["a", "b"]


@pytest.mark.asyncio
async def test_async_gen_nonstreaming_text_only_honors_finish_reason():
    async def handler(messages, options):
        yield {"delta": "hi"}
        yield {"done": True, "finish_reason": "length"}

    ws = _FakeWS()
    env = Envelope(type="chat-request", payload={"messages": []})
    await _handle_chat(_Pub(handler), ws, env)

    p = _response(ws)
    assert p["text"] == "hi"
    # Real finish reason is carried, not the hardcoded "stop".
    assert p["finish_reason"] == "length"
    # No tool calls means no tool_calls key (so the hub's gate stays off).
    assert "tool_calls" not in p


@pytest.mark.asyncio
async def test_sync_gen_nonstreaming_accumulates_tool_calls():
    def handler(messages, options):
        yield {"tool_call_delta": {"index": 0, "id": "call_z",
                                   "function": {"name": "ping",
                                                "arguments": "{}"}}}
        yield {"done": True, "finish_reason": "tool_calls"}

    ws = _FakeWS()
    env = Envelope(type="chat-request", payload={"messages": []})
    await _handle_chat(_Pub(handler), ws, env)

    p = _response(ws)
    assert p["finish_reason"] == "tool_calls"
    assert p["tool_calls"] == [{
        "id": "call_z",
        "function": {"name": "ping", "arguments": "{}"},
    }]
