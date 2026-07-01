"""Regression: _handle_chat's streaming paths (async-gen + sync-gen) hardcoded
`finish_reason="stop"` on the terminator chat-chunk regardless of what the
handler yielded. A handler that flagged a max-tokens truncation with
`finish_reason="length"` had it silently rewritten to `"stop"` on the wire,
so the client couldn't distinguish clean stop from truncation.

The non-streaming accumulate paths already carry the handler's finish_reason
through `_finalize_accumulated`. The JS port was fixed for the streaming
paths in 7427ed4 — this brings Python back to parity.
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


def _last_chunk(ws: _FakeWS) -> dict:
    """The terminator envelope — the last chat-chunk on the wire."""
    chunks = [e for e in ws.sent if e["type"] == "chat-chunk"]
    assert chunks, f"no chat-chunk envelopes emitted; got {[e['type'] for e in ws.sent]}"
    return chunks[-1]


@pytest.mark.asyncio
async def test_asyncgen_streaming_terminator_echoes_handler_finish_reason():
    """Handler yields a final chunk with finish_reason='length'. The terminator
    chat-chunk (done=True) must carry finish_reason='length', not 'stop'."""

    async def handler(messages, options):
        yield "hello "
        yield "world"
        yield {"delta": "", "finish_reason": "length"}

    ws = _FakeWS()
    env = Envelope(
        type="chat-request",
        payload={"messages": [], "stream": True},
    )
    await _handle_chat(_Pub(handler), ws, env)

    terminator = _last_chunk(ws)
    assert terminator["payload"]["done"] is True
    assert terminator["payload"]["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_syncgen_streaming_terminator_echoes_handler_finish_reason():
    """Same guarantee for a sync generator handler."""

    def handler(messages, options):
        yield "partial "
        yield "answer"
        yield {"delta": "", "finish_reason": "length"}

    ws = _FakeWS()
    env = Envelope(
        type="chat-request",
        payload={"messages": [], "stream": True},
    )
    await _handle_chat(_Pub(handler), ws, env)

    terminator = _last_chunk(ws)
    assert terminator["payload"]["done"] is True
    assert terminator["payload"]["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_asyncgen_streaming_defaults_to_stop_when_handler_silent():
    """Handler never sets finish_reason → terminator carries the default 'stop'.
    Regression guard so the fix doesn't drop the pre-existing default."""

    async def handler(messages, options):
        yield "one"
        yield "two"

    ws = _FakeWS()
    env = Envelope(
        type="chat-request",
        payload={"messages": [], "stream": True},
    )
    await _handle_chat(_Pub(handler), ws, env)

    terminator = _last_chunk(ws)
    assert terminator["payload"]["done"] is True
    assert terminator["payload"]["finish_reason"] == "stop"
