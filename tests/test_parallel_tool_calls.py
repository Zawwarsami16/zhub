"""Phase 1.8b — Parallel tool calls.

A single chat-response from the publisher carries multiple tool_calls. The
hub resolves them concurrently (not in series) and appends one role=tool
message per call before re-asking the publisher. Final response stitches
all results.
"""

import asyncio
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
    """Publisher emits two tool_calls in one response. The hub must invoke
    both capabilities concurrently, not serially.

    Concurrency is verified by recording when each handler starts: if
    slow_a and slow_b both start within 0.4s of each other they were
    running as overlapping asyncio tasks (the serial case would have a
    gap equal to the first handler's full sleep, ~0.5s). Wall-clock
    total time is not used — it is too sensitive to scheduler noise on
    shared VPS runners.
    """
    hub_ws = f"ws://127.0.0.1:{parallel_hub_port}"
    hub_http = f"http://127.0.0.1:{parallel_hub_port}"
    call_n = {"n": 0}
    starts: dict[str, float] = {}

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
        starts["a"] = time.monotonic()
        await asyncio.sleep(0.5)
        return {"from": "a"}

    async def slow_b(_args):
        starts["b"] = time.monotonic()
        await asyncio.sleep(0.5)
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
    async with httpx.AsyncClient(timeout=15.0) as client:
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

    # Verify concurrency: both handlers must have run, and must have started
    # within 0.4s of each other. Serial execution would have a gap >= the
    # first handler's sleep (0.5s); concurrent execution has a gap of ~0ms.
    assert "a" in starts and "b" in starts, "both capability handlers must have been invoked"
    start_gap = abs(starts["a"] - starts["b"])
    assert start_gap < 0.4, (
        f"handlers started {start_gap:.3f}s apart — "
        f"hub likely invoked them serially (gap must be < first sleep = 0.5s)"
    )
    # Loose wall-clock bound: catch hangs, not timing noise.
    assert elapsed < 8.0, f"request took {elapsed:.2f}s — something appears hung"
