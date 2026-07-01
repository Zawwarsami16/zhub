"""Regression: a chat_handler that forgets ``return`` (returns None from
falling off the end of the function body) shipped the literal three-character
string ``"None"`` as the assistant reply text — the single-shot else branch
called ``str(result)`` on the None value.

This is a common publisher-side mistake: ``def handler(msgs, opts): print(msgs)``
runs cleanly, returns None. Before the fix the caller saw ``{"text": "None"}``
on the wire; after the fix an empty response goes out, matching the JS port's
``String(awaited ?? '')`` behavior.
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
async def test_sync_handler_returning_none_emits_empty_text():
    def handler(messages, options):
        # Common mistake: forgot the return statement. Body runs, function
        # falls off the end, returns None.
        pass

    ws = _FakeWS()
    env = Envelope(type="chat-request", request_id="r1", payload={"messages": []})
    await _handle_chat(_Pub(handler), ws, env)

    assert len(ws.sent) == 1
    payload = ws.sent[0]["payload"]
    assert payload["text"] == ""  # pre-fix: "None"
    assert payload["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_async_handler_returning_none_emits_empty_text():
    async def handler(messages, options):
        return None

    ws = _FakeWS()
    env = Envelope(type="chat-request", request_id="r2", payload={"messages": []})
    await _handle_chat(_Pub(handler), ws, env)

    assert ws.sent[-1]["payload"]["text"] == ""


@pytest.mark.asyncio
async def test_int_result_still_stringified():
    """Guard: the fix only special-cases None. A handler returning a non-None
    non-str non-dict value (int, float, list …) is still stringified — this
    keeps existing behavior for the two-year-stable else branch, and only
    the ``str(None) == 'None'`` embarrassment is corrected."""

    def handler(messages, options):
        return 42

    ws = _FakeWS()
    env = Envelope(type="chat-request", request_id="r3", payload={"messages": []})
    await _handle_chat(_Pub(handler), ws, env)

    assert ws.sent[-1]["payload"]["text"] == "42"
