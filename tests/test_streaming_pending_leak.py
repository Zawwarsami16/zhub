"""Regression test: publisher.pending queue entry must be removed after streaming.

When a streaming HTTP request is handled, proxy_chat(stream=True) plants an
asyncio.Queue in publisher.pending[request_id] and returns immediately. The
hub's WS handler then drains chunks from the publisher into that queue.

Pre-fix behaviour: the queue entry was NEVER removed from publisher.pending.
Every streaming request leaked one Queue object that persisted until the
publisher disconnected. Long-running publishers accumulated dead entries.

Fix: the WS handler pops from publisher.pending after emitting the done
sentinel (None) in both the chat-chunk path and the chat-response→Queue path.

These tests cover both paths with a real hub + publisher combination so the
actual handler code runs (not a mock).
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
from zhub import publish


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _parse_sse(body: str) -> list[dict]:
    out: list[dict] = []
    for line in body.splitlines():
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


@pytest.fixture(scope="module")
def hub_with_state():
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
    yield port, app


@pytest.mark.asyncio
async def test_streaming_chunk_pending_cleaned_after_response(hub_with_state):
    """chat-chunk path: publisher pending Queue must be removed after streaming ends.

    The publisher uses a generator (yields chunk-by-chunk). The hub forwards
    each as a chat-chunk WS message. On the final chunk (done=True), the fix
    pops the queue from publisher.pending. Pre-fix: the queue persisted.
    """
    port, app = hub_with_state
    hub = app.state.hub

    def streaming_handler(messages, options):
        yield "hello "
        yield "world"

    pub = publish(
        name="pending-stream-bot",
        description="pending leak test (streaming)",
        chat_handler=streaming_handler,
        hub_url=f"ws://127.0.0.1:{port}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"http://127.0.0.1:{port}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 200
    chunks = _parse_sse(resp.text)
    assert any("hello" in (c["choices"][0].get("delta", {}).get("content", "") or "") for c in chunks)

    # Allow a brief moment for the WS handler to process the final done chunk
    # and run its cleanup.
    for _ in range(20):
        reg = hub.publishers.get(pub.name)
        if reg and not reg.pending:
            break
        await asyncio.sleep(0.05)

    reg = hub.publishers.get(pub.name)
    assert reg is not None, "publisher unregistered unexpectedly"
    assert not reg.pending, (
        f"publisher.pending not empty after streaming done: {list(reg.pending.keys())}"
    )


@pytest.mark.asyncio
async def test_nonstreaming_pub_streaming_client_pending_cleaned(hub_with_state):
    """chat-response→Queue path: publisher pending Queue must be removed when a
    non-streaming publisher responds to a streaming (SSE) HTTP client.

    The publisher returns a plain string (chat-response WS message). The hub
    converts it to a Queue + sentinel for the SSE client. On putting the sentinel
    the fix pops the queue from publisher.pending. Pre-fix: the queue persisted.
    """
    port, app = hub_with_state
    hub = app.state.hub

    def simple_handler(messages, options):
        return "done"

    pub = publish(
        name="pending-nonstream-bot",
        description="pending leak test (non-streaming pub, streaming client)",
        chat_handler=simple_handler,
        hub_url=f"ws://127.0.0.1:{port}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"http://127.0.0.1:{port}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 200
    chunks = _parse_sse(resp.text)
    assert any("done" in (c["choices"][0].get("delta", {}).get("content", "") or "") for c in chunks)

    for _ in range(20):
        reg = hub.publishers.get(pub.name)
        if reg and not reg.pending:
            break
        await asyncio.sleep(0.05)

    reg = hub.publishers.get(pub.name)
    assert reg is not None, "publisher unregistered unexpectedly"
    assert not reg.pending, (
        f"publisher.pending not empty after streaming done: {list(reg.pending.keys())}"
    )
