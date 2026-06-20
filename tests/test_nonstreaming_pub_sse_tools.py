"""Regression: single-shot publisher tool_calls dropped for streaming SSE caller.

A single-shot chat_handler (returns dict, not a generator) sends a
`chat-response` envelope. When the HTTP caller requested streaming,
the hub had a Queue in publisher.pending and converted the response to
streaming chunks. The old conversion emitted only `delta` + `done` —
dropping `tool_calls` entirely. The SSE event_stream then saw
finish_reason=="tool_calls" with an empty accumulated_tool_calls dict,
so auto-resolution never fired (silent capability loss).

Fix: the conversion now emits each tool call as a `tool_call_delta`
chunk (same shape the streaming accumulator expects) before the done
chunk, matching what a streaming publisher would have sent.
"""

import asyncio
import json
import socket
import threading
import time

import pytest

try:
    import fastapi  # noqa
    import uvicorn  # noqa
    import httpx  # noqa
    DEPS_AVAILABLE = True
except ImportError:
    DEPS_AVAILABLE = False

if DEPS_AVAILABLE:
    from zhub.server import create_app
from zhub import publish, connect


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def hub():
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    port = _free_port()
    app = create_app()

    def run():
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        asyncio.run(uvicorn.Server(config).serve())

    threading.Thread(target=run, daemon=True).start()
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)
    yield port


def _parse_sse(body: str) -> list[dict]:
    out = []
    for line in body.splitlines():
        line = line.rstrip("\r")
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            out.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return out


@pytest.mark.asyncio
async def test_nonstreaming_pub_tool_calls_forwarded_in_sse(hub):
    """Single-shot publisher returns tool_calls. SSE caller must receive them
    as tool_call_delta chunks, NOT silently dropped finish_reason:tool_calls."""

    def chat_handler(messages, options):
        return {
            "text": "",
            "finish_reason": "tool_calls",
            "tool_calls": [{
                "id": "call_ns1",
                "type": "function",
                "function": {"name": "ns_thing", "arguments": json.dumps({"q": 1})},
            }],
        }

    pub = publish(
        name="ns-tc-bot",
        description="non-streaming publisher tool_call regression",
        chat_handler=chat_handler,
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key, "publisher did not register"

    async with httpx.AsyncClient(timeout=5.0) as c:
        resp = await c.post(
            f"http://127.0.0.1:{hub}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "go"}], "stream": True},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 200
    chunks = _parse_sse(resp.text)
    assert chunks, f"empty SSE body: {resp.text!r}"

    # At least one SSE chunk must carry tool_calls in its delta.
    tc_chunks = [
        c for c in chunks
        if c.get("choices", [{}])[0].get("delta", {}).get("tool_calls")
    ]
    assert tc_chunks, (
        "tool_calls were silently dropped (regression): "
        f"no tool_call_delta SSE chunk found in {chunks!r}"
    )
    tcd = tc_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
    assert tcd.get("function", {}).get("name") == "ns_thing"

    finish = next(
        (c["choices"][0].get("finish_reason") for c in reversed(chunks)
         if c["choices"][0].get("finish_reason")),
        None,
    )
    assert finish == "tool_calls"


@pytest.mark.asyncio
async def test_nonstreaming_pub_auto_mode_resolves_tool_calls(hub):
    """Auto mode with a single-shot publisher: tool_calls must be forwarded so
    the SSE accumulator fires the capability and streams the follow-up reply."""
    invoke_n = {"n": 0}
    call_n = {"n": 0}

    def chat_handler(messages, options):
        call_n["n"] += 1
        if call_n["n"] == 1:
            return {
                "text": "",
                "finish_reason": "tool_calls",
                "tool_calls": [{
                    "id": "call_ns2",
                    "type": "function",
                    "function": {"name": "ns_auto_thing", "arguments": json.dumps({"x": 2})},
                }],
            }
        return "resolved ok"

    def thing_handler(args):
        invoke_n["n"] += 1
        return {"result": "fired", "got": args}

    pub = publish(
        name="ns-auto-bot",
        description="non-streaming publisher auto-mode regression",
        chat_handler=chat_handler,
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key, "publisher did not register"

    conn = connect(
        ai_name=pub.name, api_key=pub.api_key,
        hub_url=f"ws://127.0.0.1:{hub}",
        capabilities={"ns_auto_thing": ({"type": "object"}, thing_handler)},
    )
    for _ in range(60):
        if pub.find_capability("ns_auto_thing") is not None:
            break
        await asyncio.sleep(0.1)
    assert pub.find_capability("ns_auto_thing") is not None, "connection never established"

    async with httpx.AsyncClient(timeout=10.0) as c:
        resp = await c.post(
            f"http://127.0.0.1:{hub}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "go"}], "stream": True},
            headers={
                "Authorization": f"Bearer {pub.api_key}",
                "X-Zhub-Stream-Tools": "auto",
            },
        )
    assert resp.status_code == 200
    chunks = _parse_sse(resp.text)
    assert chunks

    text = "".join(
        c["choices"][0].get("delta", {}).get("content", "") or ""
        for c in chunks
    )
    assert "resolved ok" in text, f"follow-up text missing: {text!r}"
    assert invoke_n["n"] == 1, "capability did not fire"
    assert call_n["n"] == 2, "publisher not re-called after resolution"
