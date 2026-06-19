"""Phase 4.2 — pre-resolve mode for tool calls in streaming responses.

When client sends `stream:true` AND `X-Zhub-Stream-Tools: pre-resolve`,
the hub runs the non-streaming auto-resolve path internally so any
tool_calls the publisher emits are resolved before the SSE stream
starts. The final text is emitted as one SSE chunk + done.

Default streaming (no header) keeps today's behavior: text chunks
forwarded as SSE, tool_calls (if any) ignored on the streaming path.
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
def stream_hub_port():
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
    """Pull all `data: {...}` JSON chunks out of an SSE response, skipping
    `[DONE]`."""
    out: list[dict] = []
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
async def test_stream_with_preresolve_header_runs_tool_calls(stream_hub_port):
    """Publisher emits tool_calls on first turn, plain text on second.
    Connected client exposes the capability. Stream + pre-resolve header
    should auto-resolve and surface the final text in the SSE stream."""
    hub_ws = f"ws://127.0.0.1:{stream_hub_port}"
    hub_http = f"http://127.0.0.1:{stream_hub_port}"
    invoke_n = {"n": 0}
    call_n = {"n": 0}

    def chat_handler(messages, options):
        call_n["n"] += 1
        if call_n["n"] == 1:
            return {
                "text": "",
                "tool_calls": [{
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "do_thing",
                                 "arguments": json.dumps({"x": 1})},
                }],
                "finish_reason": "tool_calls",
            }
        last_tool = next(
            (m for m in reversed(messages) if m.get("role") == "tool"),
            None,
        )
        return f"final answer using tool: {last_tool['content']}"

    def thing_handler(args):
        invoke_n["n"] += 1
        return {"result": "tool-fired", "got": args}

    pub = publish(
        name="stream-tool-bot",
        description="stream pre-resolve test",
        chat_handler=chat_handler,
        hub_url=hub_ws,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key

    conn = connect(
        ai_name=pub.name, api_key=pub.api_key, hub_url=hub_ws,
        capabilities={"do_thing": ({"type": "object"}, thing_handler)},
    )
    for _ in range(60):
        if pub.find_capability("do_thing") is not None:
            break
        await asyncio.sleep(0.1)
    assert pub.find_capability("do_thing") is not None, "connection never established"

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{hub_http}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "go"}],
                  "stream": True},
            headers={
                "Authorization": f"Bearer {pub.api_key}",
                "X-Zhub-Stream-Tools": "pre-resolve",
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("content-type", "").startswith("text/event-stream")

    chunks = _parse_sse(resp.text)
    assert chunks, f"no SSE chunks parsed: {resp.text!r}"
    accumulated = "".join(
        c["choices"][0].get("delta", {}).get("content", "") or ""
        for c in chunks
    )
    assert "final answer using tool" in accumulated, accumulated
    assert "tool-fired" in accumulated
    # The last chunk should carry finish_reason
    finish = next(
        (c["choices"][0].get("finish_reason") for c in reversed(chunks)
         if c["choices"][0].get("finish_reason")),
        None,
    )
    assert finish == "stop"
    assert invoke_n["n"] == 1
    assert call_n["n"] == 2


@pytest.mark.asyncio
async def test_stream_without_header_keeps_current_behavior(stream_hub_port):
    """No header → publisher just streams its text. Tool calls (if any)
    flow through whatever today's path supports — for a plain-text
    publisher, the SSE stream just has the text."""
    hub_ws = f"ws://127.0.0.1:{stream_hub_port}"
    hub_http = f"http://127.0.0.1:{stream_hub_port}"

    def chat_handler(messages, options):
        return "hello world"

    pub = publish(
        name="stream-plain-bot",
        description="plain stream test",
        chat_handler=chat_handler,
        hub_url=hub_ws,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{hub_http}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}],
                  "stream": True},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 200
    chunks = _parse_sse(resp.text)
    accumulated = "".join(
        c["choices"][0].get("delta", {}).get("content", "") or ""
        for c in chunks
    )
    assert "hello world" in accumulated
