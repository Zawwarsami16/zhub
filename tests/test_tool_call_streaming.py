"""Phase 4.2b — true chunked tool_call delta streaming through SSE.

Three modes:
  default               — pass tool_call deltas through verbatim, no resolve
  X-Zhub-Stream-Tools: pre-resolve   — Phase 4.2 buffer-then-resolve (unchanged)
  X-Zhub-Stream-Tools: auto          — pass through + auto-resolve when
                                       finish_reason: tool_calls arrives,
                                       continue stream with the follow-up
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
        config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                log_level="warning")
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
async def test_default_mode_passes_tool_call_deltas_through(hub):
    """Publisher (async-gen) yields a tool_call delta dict, then a finish.
    Default streaming SSE should contain the tool_call delta + finish_reason
    without the hub running auto-resolve."""

    async def chat_handler(messages, options):
        # Yield a tool_call delta, then a finish marker
        yield {
            "tool_call_delta": {
                "index": 0,
                "id": "call_x",
                "type": "function",
                "function": {"name": "do_thing", "arguments": "{\"a\":1}"},
            },
        }
        yield {"done": True, "finish_reason": "tool_calls"}

    pub = publish(
        name="stream-tc-bot",
        description="phase 4.2b default test",
        chat_handler=chat_handler,
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        resp = await c.post(
            f"http://127.0.0.1:{hub}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "go"}],
                  "stream": True},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 200
    chunks = _parse_sse(resp.text)
    assert chunks, f"empty SSE: {resp.text!r}"

    # at least one chunk must carry tool_calls in its delta
    tcs = [c for c in chunks
           if c.get("choices", [{}])[0].get("delta", {}).get("tool_calls")]
    assert tcs, f"no tool_call delta SSE chunk found: {chunks!r}"
    tcd = tcs[0]["choices"][0]["delta"]["tool_calls"][0]
    assert tcd["function"]["name"] == "do_thing"

    # final chunk must carry finish_reason: tool_calls
    finish = next(
        (c["choices"][0].get("finish_reason") for c in reversed(chunks)
         if c["choices"][0].get("finish_reason")),
        None,
    )
    assert finish == "tool_calls"


@pytest.mark.asyncio
async def test_auto_mode_resolves_and_continues_stream(hub):
    """Publisher emits tool_call delta + finish:tool_calls on first call,
    then plain text on the follow-up. Auto mode should auto-invoke the
    connected capability, append role:tool, re-ask publisher, and stream
    the follow-up text. Final SSE should carry tool_call deltas THEN the
    follow-up text deltas."""
    invoke_n = {"n": 0}
    call_n = {"n": 0}

    async def chat_handler(messages, options):
        call_n["n"] += 1
        if call_n["n"] == 1:
            yield {
                "tool_call_delta": {
                    "index": 0,
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "auto_thing",
                                 "arguments": json.dumps({"x": 1})},
                },
            }
            yield {"done": True, "finish_reason": "tool_calls"}
        else:
            # follow-up after tool resolution: stream some text
            for piece in ("ok ", "done"):
                yield piece

    def thing_handler(args):
        invoke_n["n"] += 1
        return {"got": args, "result": "fired"}

    pub = publish(
        name="auto-tc-bot",
        description="phase 4.2b auto mode",
        chat_handler=chat_handler,
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    conn = connect(
        ai_name=pub.name, api_key=pub.api_key,
        hub_url=f"ws://127.0.0.1:{hub}",
        capabilities={"auto_thing": ({"type": "object"}, thing_handler)},
    )
    await asyncio.sleep(0.6)

    async with httpx.AsyncClient(timeout=10.0) as c:
        resp = await c.post(
            f"http://127.0.0.1:{hub}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "go"}],
                  "stream": True},
            headers={
                "Authorization": f"Bearer {pub.api_key}",
                "X-Zhub-Stream-Tools": "auto",
            },
        )
    assert resp.status_code == 200
    chunks = _parse_sse(resp.text)
    assert chunks

    # Tool_call deltas must be in the early stream
    tc_chunks = [c for c in chunks
                 if c.get("choices", [{}])[0].get("delta", {}).get("tool_calls")]
    assert tc_chunks, f"no tool_call delta seen: {chunks!r}"

    # Follow-up text must arrive after resolution
    text = "".join(
        c["choices"][0].get("delta", {}).get("content", "") or ""
        for c in chunks
    )
    assert "ok" in text and "done" in text, f"missing follow-up text: {text!r}"

    # Capability fired exactly once
    assert invoke_n["n"] == 1
    # Publisher called twice (initial + follow-up)
    assert call_n["n"] == 2

    # Final finish_reason should be "stop" (the follow-up's)
    finish = next(
        (c["choices"][0].get("finish_reason") for c in reversed(chunks)
         if c["choices"][0].get("finish_reason")),
        None,
    )
    assert finish == "stop"


@pytest.mark.asyncio
async def test_combined_tool_call_done_chunk_keeps_finish_reason(hub):
    """Regression: a publisher may flag its last tool_call delta with done +
    finish_reason in ONE chunk (a shape _serialize_stream_chunk supports and
    the hub relays verbatim), instead of a separate trailing finish chunk. The
    SSE consumer must still surface finish_reason: tool_calls — not silently
    drop it because the tool_call branch short-circuits the done check."""

    async def chat_handler(messages, options):
        # delta + done + finish_reason all in a single envelope
        yield {
            "tool_call_delta": {
                "index": 0,
                "id": "call_combined",
                "type": "function",
                "function": {"name": "do_thing", "arguments": "{\"a\":1}"},
            },
            "done": True,
            "finish_reason": "tool_calls",
        }

    pub = publish(
        name="combined-tc-bot",
        description="combined tool_call+done chunk",
        chat_handler=chat_handler,
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        resp = await c.post(
            f"http://127.0.0.1:{hub}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "go"}],
                  "stream": True},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 200
    chunks = _parse_sse(resp.text)
    assert chunks, f"empty SSE: {resp.text!r}"

    tcs = [c for c in chunks
           if c.get("choices", [{}])[0].get("delta", {}).get("tool_calls")]
    assert tcs, f"no tool_call delta SSE chunk found: {chunks!r}"

    finish = next(
        (c["choices"][0].get("finish_reason") for c in reversed(chunks)
         if c["choices"][0].get("finish_reason")),
        None,
    )
    assert finish == "tool_calls", (
        f"combined tool_call+done chunk lost finish_reason: {chunks!r}"
    )


@pytest.mark.asyncio
async def test_auto_mode_resolves_combined_tool_call_done_chunk(hub):
    """Auto mode must auto-resolve even when the tool_call delta and its
    done/finish:tool_calls ride in a single combined chunk. Before the fix the
    tool_call branch `continue`d past the done check, final_finish stayed
    'stop', and the capability never fired."""
    invoke_n = {"n": 0}
    call_n = {"n": 0}

    async def chat_handler(messages, options):
        call_n["n"] += 1
        if call_n["n"] == 1:
            yield {
                "tool_call_delta": {
                    "index": 0,
                    "id": "call_ac",
                    "type": "function",
                    "function": {"name": "auto_combined",
                                 "arguments": json.dumps({"x": 1})},
                },
                "done": True,
                "finish_reason": "tool_calls",
            }
        else:
            for piece in ("ok ", "done"):
                yield piece

    def thing_handler(args):
        invoke_n["n"] += 1
        return {"got": args, "result": "fired"}

    pub = publish(
        name="auto-combined-bot",
        description="auto mode combined chunk",
        chat_handler=chat_handler,
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    conn = connect(
        ai_name=pub.name, api_key=pub.api_key,
        hub_url=f"ws://127.0.0.1:{hub}",
        capabilities={"auto_combined": ({"type": "object"}, thing_handler)},
    )
    await asyncio.sleep(0.6)

    async with httpx.AsyncClient(timeout=10.0) as c:
        resp = await c.post(
            f"http://127.0.0.1:{hub}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "go"}],
                  "stream": True},
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
    assert "ok" in text and "done" in text, f"missing follow-up text: {text!r}"
    assert invoke_n["n"] == 1, "capability did not fire on combined chunk"
    assert call_n["n"] == 2, "publisher follow-up not requested"


@pytest.mark.asyncio
async def test_pre_resolve_mode_still_works(hub):
    """Phase 4.2 pre-resolve path is untouched."""
    call_n = {"n": 0}

    def chat_handler(messages, options):
        call_n["n"] += 1
        if call_n["n"] == 1:
            return {
                "text": "",
                "tool_calls": [{
                    "id": "p1",
                    "type": "function",
                    "function": {"name": "pre_thing", "arguments": "{}"},
                }],
                "finish_reason": "tool_calls",
            }
        return "preresolved final"

    pub = publish(
        name="pre-tc-bot",
        description="phase 4.2 pre-resolve sanity",
        chat_handler=chat_handler,
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    conn = connect(
        ai_name=pub.name, api_key=pub.api_key,
        hub_url=f"ws://127.0.0.1:{hub}",
        capabilities={"pre_thing": ({"type": "object"}, lambda a: {"ok": True})},
    )
    await asyncio.sleep(0.6)

    async with httpx.AsyncClient(timeout=8.0) as c:
        resp = await c.post(
            f"http://127.0.0.1:{hub}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "go"}],
                  "stream": True},
            headers={
                "Authorization": f"Bearer {pub.api_key}",
                "X-Zhub-Stream-Tools": "pre-resolve",
            },
        )
    assert resp.status_code == 200
    text = "".join(
        c["choices"][0].get("delta", {}).get("content", "") or ""
        for c in _parse_sse(resp.text)
    )
    assert "preresolved final" in text
