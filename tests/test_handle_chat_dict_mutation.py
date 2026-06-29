"""Regression: _handle_chat single-shot dict path used `payload = result` and
then `setdefault`'d defaults onto the handler-supplied dict, mutating a
caller's template / cached response (silently adding `text: ""` and
`finish_reason: "stop"`). A handler returning a class-attribute or a memoised
response dict therefore saw the SAME dict grow extra keys across calls.

Fix copies first (`payload = dict(result)`); these tests pin that behavior.
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
async def test_dict_handler_result_is_not_mutated_with_defaults():
    template = {"tool_calls": [{"id": "c1", "function": {"name": "lookup"}}]}

    def handler(messages, options):
        return template

    ws = _FakeWS()
    env = Envelope(type="chat-request", payload={"messages": []})
    await _handle_chat(_Pub(handler), ws, env)

    # Wire payload still carries the defaults the hub expects.
    payload = ws.sent[0]["payload"]
    assert payload["text"] == ""
    assert payload["finish_reason"] == "stop"
    assert payload["tool_calls"] == [{"id": "c1", "function": {"name": "lookup"}}]

    # But the caller's template is untouched — same keys it walked in with.
    assert set(template.keys()) == {"tool_calls"}


@pytest.mark.asyncio
async def test_dict_handler_text_present_no_default_inserted():
    template = {"text": "hi", "usage": {"prompt_tokens": 7}}

    def handler(messages, options):
        return template

    ws = _FakeWS()
    env = Envelope(type="chat-request", payload={"messages": []})
    await _handle_chat(_Pub(handler), ws, env)

    payload = ws.sent[0]["payload"]
    assert payload["text"] == "hi"
    assert payload["finish_reason"] == "stop"
    assert payload["usage"] == {"prompt_tokens": 7}

    # Caller's template unchanged across the call.
    assert set(template.keys()) == {"text", "usage"}


@pytest.mark.asyncio
async def test_dict_handler_reused_across_calls_keeps_clean_keys():
    """Two calls reusing the same dict — second call must not see the first
    call's defaults baked into the template."""
    template = {"tool_calls": []}

    def handler(messages, options):
        return template

    pub = _Pub(handler)
    ws1 = _FakeWS()
    ws2 = _FakeWS()
    await _handle_chat(pub, ws1, Envelope(type="chat-request", payload={"messages": []}))
    snapshot_after_first = set(template.keys())
    await _handle_chat(pub, ws2, Envelope(type="chat-request", payload={"messages": []}))

    assert snapshot_after_first == {"tool_calls"}
    assert set(template.keys()) == {"tool_calls"}
    # Both wire payloads still carry the defaults.
    assert ws1.sent[0]["payload"]["text"] == ""
    assert ws2.sent[0]["payload"]["text"] == ""
