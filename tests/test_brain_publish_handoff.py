"""The brain → publisher hand-off must preserve tool calls.

`stream_for_publish` is what both publisher entry points (`zhub up` and
examples/multi_brain_publisher.py) use to turn a brain adapter's stream
into the chunks a zhub chat_handler yields. Every bespoke adapter was
taught to surface OpenAI-shape tool_call deltas + a
``finish_reason="tool_calls"`` terminator, but a handler that yields only
``chunk.delta`` throws all of that away — so a tool-call turn reaches the
hub as empty text with finish_reason "stop" and auto-resolution never
fires. These tests pin the hand-off on both the streaming and the
non-streaming publisher paths.
"""

from __future__ import annotations

import json

import pytest

from zhub.brains import stream_for_publish
from zhub.brains.base import BrainAdapter, ChatChunk
from zhub.client import (
    _accumulate_tool_call,
    _chunk_fields,
    _finalize_accumulated,
    _serialize_stream_chunk,
)


class _ToolCallBrain(BrainAdapter):
    """A brain that answers a turn with a single tool call: one text
    preamble delta, a tool_call opener (empty delta), an argument
    fragment (empty delta), then a final empty chunk carrying
    finish_reason="tool_calls". Records the tools it was handed."""

    name = "toolfake"
    label = "tool-call fake brain"

    def __init__(self) -> None:
        self.seen_tools = None

    @classmethod
    def try_init(cls):
        return cls()

    async def stream(self, messages, *, system=None, temperature=0.7,
                     max_tokens=2048, tools=None):
        self.seen_tools = tools
        yield ChatChunk(delta="let me check ")
        yield ChatChunk(delta="", tool_call_delta={
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "get_weather"},
        })
        yield ChatChunk(delta="", tool_call_delta={
            "index": 0, "function": {"arguments": '{"city":"Paris"}'},
        })
        yield ChatChunk(delta="", done=True, finish_reason="tool_calls")


async def _collect(brain, **kw):
    return [c async for c in stream_for_publish(brain, [{"role": "user", "content": "hi"}], **kw)]


@pytest.mark.asyncio
async def test_tool_call_chunks_are_not_dropped():
    brain = _ToolCallBrain()
    chunks = await _collect(brain)

    # The text delta survives.
    assert any(c.delta == "let me check " for c in chunks)
    # Both tool_call deltas survive (the bug dropped these — empty delta).
    tcds = [c.tool_call_delta for c in chunks if c.tool_call_delta]
    assert len(tcds) == 2
    assert tcds[0]["function"]["name"] == "get_weather"
    assert tcds[1]["function"]["arguments"] == '{"city":"Paris"}'
    # The tool_calls finish_reason survives.
    assert any(c.finish_reason == "tool_calls" for c in chunks)


@pytest.mark.asyncio
async def test_tools_are_forwarded_to_the_brain():
    brain = _ToolCallBrain()
    tools = [{"type": "function", "function": {"name": "get_weather"}}]
    await _collect(brain, tools=tools)
    assert brain.seen_tools == tools


@pytest.mark.asyncio
async def test_streaming_serialization_carries_tool_calls_to_hub():
    """Serialize each yielded chunk the way the publisher's streaming
    path does, then run the envelopes through the hub's streaming
    accumulator logic — a tool call + tool_calls finish must come out."""
    brain = _ToolCallBrain()
    chunks = await _collect(brain)

    accumulated: dict[int, dict] = {}
    final_finish = "stop"
    for chunk in chunks:
        payload = json.loads(_serialize_stream_chunk(chunk, "req"))["payload"]
        tcd = payload.get("tool_call_delta")
        if tcd:
            _accumulate_tool_call(accumulated, tcd)
        if payload.get("done"):
            final_finish = payload.get("finish_reason") or "stop"

    assert final_finish == "tool_calls"
    assert accumulated[0]["function"]["name"] == "get_weather"
    assert accumulated[0]["function"]["arguments"] == '{"city":"Paris"}'


@pytest.mark.asyncio
async def test_nonstreaming_accumulation_carries_tool_calls_to_hub():
    """The non-streaming publisher path folds the same chunks into one
    chat-response; tool_calls and the real finish_reason must be on it."""
    brain = _ToolCallBrain()
    chunks = await _collect(brain)

    text_parts: list[str] = []
    slots: dict[int, dict] = {}
    finish = None
    for chunk in chunks:
        text, tcd, fin = _chunk_fields(chunk)
        if text:
            text_parts.append(text)
        if tcd:
            _accumulate_tool_call(slots, tcd)
        if fin:
            finish = fin
    payload = _finalize_accumulated(text_parts, slots, finish)

    assert payload["finish_reason"] == "tool_calls"
    assert payload["tool_calls"][0]["function"]["name"] == "get_weather"
    assert payload["tool_calls"][0]["function"]["arguments"] == '{"city":"Paris"}'
    assert payload["text"] == "let me check "
