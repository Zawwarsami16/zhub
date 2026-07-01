"""Regression: `_handle_chat` awaited a coroutine chat_handler once and then
fell into the single-shot path — but an ``async def`` handler that itself
returns an async (or sync) generator resolved to a *generator*, which then
got stringified into ``"<async_generator object …>"`` as the chat-response
text. The isasyncgen / isgenerator branches only ran against the raw
handler result, before the coroutine was awaited.

Reachable on any publisher writing::

    async def handler(messages, options):
        return _stream()  # _stream is an async or sync generator

which is the natural pattern once the handler needs to do async setup
(open an httpx client, load a key, look up state) before returning the
stream. The fix is to await the coroutine FIRST, then run the same
gen-detection against the awaited value.
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


@pytest.mark.asyncio
async def test_async_def_returning_asyncgen_accumulates_correctly():
    """Non-streaming: text parts from the returned async-gen must join into
    the chat-response text field (was ``"<async_generator …>"`` pre-fix)."""

    async def handler(messages, options):
        async def _gen():
            yield "hi "
            yield "there"
            yield {"delta": "", "finish_reason": "length"}
        return _gen()

    ws = _FakeWS()
    env = Envelope(type="chat-request", payload={"messages": []})
    await _handle_chat(_Pub(handler), ws, env)

    assert len(ws.sent) == 1
    payload = ws.sent[0]["payload"]
    assert payload["text"] == "hi there"
    assert payload["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_async_def_returning_asyncgen_streams():
    """Streaming: the returned async-gen must emit chat-chunks per yield,
    then a terminator carrying the handler's finish_reason. Pre-fix the
    single-shot path emitted one chat-response with the stringified gen."""

    async def handler(messages, options):
        async def _gen():
            yield "one "
            yield "two"
        return _gen()

    ws = _FakeWS()
    env = Envelope(
        type="chat-request",
        payload={"messages": [], "stream": True},
    )
    await _handle_chat(_Pub(handler), ws, env)

    chunks = [e for e in ws.sent if e["type"] == "chat-chunk"]
    assert [c["payload"].get("delta") for c in chunks if not c["payload"]["done"]] == ["one ", "two"]
    terminator = chunks[-1]
    assert terminator["payload"]["done"] is True
    assert terminator["payload"]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_async_def_returning_syncgen_accumulates_correctly():
    """Sync-generator variant of the same bug — an ``async def`` handler
    returning a plain generator function's iterator."""

    async def handler(messages, options):
        def _gen():
            yield "a"
            yield "b"
            yield "c"
        return _gen()

    ws = _FakeWS()
    env = Envelope(type="chat-request", payload={"messages": []})
    await _handle_chat(_Pub(handler), ws, env)

    assert len(ws.sent) == 1
    assert ws.sent[0]["payload"]["text"] == "abc"


@pytest.mark.asyncio
async def test_async_def_returning_string_still_single_shot():
    """Regression guard: the standard ``async def`` handler that returns a
    string must keep working as a single-shot chat-response — the fix must
    not eat the coroutine path for the common case."""

    async def handler(messages, options):
        return "plain reply"

    ws = _FakeWS()
    env = Envelope(type="chat-request", payload={"messages": []})
    await _handle_chat(_Pub(handler), ws, env)

    assert len(ws.sent) == 1
    payload = ws.sent[0]["payload"]
    assert payload["text"] == "plain reply"
    assert payload["finish_reason"] == "stop"
