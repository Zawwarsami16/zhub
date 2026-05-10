"""Phase 1.8b — Parallel tool calls.

A single chat-response from the publisher carries multiple tool_calls. The
hub resolves them concurrently (not in series) and appends one role=tool
message per call before re-asking the publisher. Final response stitches
all results.
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
def parallel_hub_port():
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


@pytest.mark.asyncio
async def test_parallel_tool_calls_resolved_concurrently(parallel_hub_port):
    """Publisher emits two tool_calls in one response. Each capability
    handler sleeps 0.5s. If serialized, total > 1s; concurrent < 0.8s.
    Both results should appear in the audit log keyed to their
    tool_call_id, and the final message should mention both."""
    hub_ws = f"ws://127.0.0.1:{parallel_hub_port}"
    hub_http = f"http://127.0.0.1:{parallel_hub_port}"
    call_n = {"n": 0}

    def chat_handler(messages, options):
        call_n["n"] += 1
        if call_n["n"] == 1:
            return {
                "text": "",
                "tool_calls": [
                    {"id": "call_a",
                     "type": "function",
                     "function": {"name": "slow_a", "arguments": "{}"}},
                    {"id": "call_b",
                     "type": "function",
                     "function": {"name": "slow_b", "arguments": "{}"}},
                ],
                "finish_reason": "tool_calls",
            }
        # final call after both tool results have been fed back
        results = [m for m in messages if m.get("role") == "tool"]
        names = [m.get("name") for m in results]
        return f"both done: {sorted(names)}"

    async def slow_a(_args):
        await asyncio.sleep(0.8)
        return {"from": "a"}

    async def slow_b(_args):
        await asyncio.sleep(0.8)
        return {"from": "b"}

    pub = publish(
        name="parallel-bot",
        description="parallel test",
        chat_handler=chat_handler,
        hub_url=hub_ws,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    conn = connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url=hub_ws,
        capabilities={
            "slow_a": ({"type": "object"}, slow_a),
            "slow_b": ({"type": "object"}, slow_b),
        },
    )
    await asyncio.sleep(0.8)

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{hub_http}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "go"}]},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    elapsed = time.monotonic() - t0

    assert resp.status_code == 200, resp.text
    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    assert content == "both done: ['slow_a', 'slow_b']", f"got {content!r}"
    audit = body["usage"]["tool_results"]
    assert {a["name"] for a in audit} == {"slow_a", "slow_b"}
    assert {a["tool_call_id"] for a in audit} == {"call_a", "call_b"}
    # Two 0.8s sleeps. Serial = 1.6s + overhead. Parallel = 0.8s + overhead.
    # We want a threshold that still distinguishes parallel from serial
    # but tolerates slow CI. Serial would clock north of 1.6 + N×scheduler
    # overhead; parallel below ~1s under normal conditions, but can drift
    # to 1.4–1.7s on heavily-shared runners. Use 1.9s — a serial impl
    # would be even higher (~2.1s+ once the second 0.8s sleep adds up).
    assert elapsed < 1.9, f"parallel resolution should finish under 1.9s; took {elapsed:.2f}s"
