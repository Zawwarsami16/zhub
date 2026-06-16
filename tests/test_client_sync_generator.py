"""Unit coverage for sync-generator chat handlers in _handle_chat.

A chat_handler may be a *sync* generator. The async-gen path serializes each
yielded chunk through _serialize_stream_chunk / _chunk_to_text, which extract
the delta (and preserve tool_call_delta / done / finish_reason) for dict- and
ChatChunk-shaped chunks. The sync-gen path historically called str(chunk),
which stringified a dict chunk into its Python repr (e.g. "{'delta': 'hi'}")
as the literal delta and dropped done/finish_reason/tool_call_delta entirely.
These tests pin the sync path to the same behavior as the async path.
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


def _payloads(ws: _FakeWS) -> list[dict]:
    return [m["payload"] for m in ws.sent]


@pytest.mark.asyncio
async def test_sync_gen_streaming_dict_chunks_extract_delta():
    def handler(messages, options):
        yield {"delta": "the "}
        yield {"delta": "fox", "done": True, "finish_reason": "length"}

    ws = _FakeWS()
    env = Envelope(type="chat-request", payload={"messages": [], "stream": True})
    await _handle_chat(_Pub(handler), ws, env)

    payloads = _payloads(ws)
    # Deltas are the real text, not the dict repr.
    assert payloads[0]["delta"] == "the "
    assert payloads[1]["delta"] == "fox"
    # The chunk's own done/finish_reason survive serialization.
    assert payloads[1]["done"] is True
    assert payloads[1]["finish_reason"] == "length"
    # Trailing synthetic terminator closes the stream.
    assert payloads[-1] == {"delta": "", "done": True, "finish_reason": "stop"}


@pytest.mark.asyncio
async def test_sync_gen_streaming_forwards_tool_call_delta():
    def handler(messages, options):
        yield {"tool_call_delta": {"index": 0, "id": "call_x"}}
        yield {"done": True, "finish_reason": "tool_calls"}

    ws = _FakeWS()
    env = Envelope(type="chat-request", payload={"messages": [], "stream": True})
    await _handle_chat(_Pub(handler), ws, env)

    payloads = _payloads(ws)
    assert payloads[0]["tool_call_delta"] == {"index": 0, "id": "call_x"}
    # The tool_call delta must not have been collapsed into a string delta.
    assert "delta" not in payloads[0]
    assert payloads[1]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_sync_gen_nonstreaming_accumulates_text_not_repr():
    def handler(messages, options):
        yield {"delta": "the "}
        yield {"delta": "fox"}

    ws = _FakeWS()
    env = Envelope(type="chat-request", payload={"messages": []})
    await _handle_chat(_Pub(handler), ws, env)

    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "chat-response"
    assert ws.sent[0]["payload"]["text"] == "the fox"


@pytest.mark.asyncio
async def test_sync_gen_string_chunks_unchanged():
    def handler(messages, options):
        yield "the "
        yield "fox"

    ws = _FakeWS()
    env = Envelope(type="chat-request", payload={"messages": [], "stream": True})
    await _handle_chat(_Pub(handler), ws, env)

    payloads = _payloads(ws)
    assert payloads[0]["delta"] == "the "
    assert payloads[1]["delta"] == "fox"
    assert payloads[-1] == {"delta": "", "done": True, "finish_reason": "stop"}
