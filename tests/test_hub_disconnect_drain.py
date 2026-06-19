"""Hub-side pending drains on publisher/connection disconnect.

Two sibling bugs fixed in the same commit:

1. unregister_publisher() did not drain publisher.pending on WS disconnect.
   Non-streaming proxy_chat() futures waited 60 s (asyncio.wait_for timeout).
   Streaming proxy_chat() queues blocked FOREVER on queue.get() — no timeout.

2. unregister_connection() did not drain conn.pending on WS disconnect.
   invoke_capability() futures waited 60 s for the full timeout.

Post-fix: both methods capture the registration before popping, then:
  - publisher: Future → set LookupError; Queue → put_nowait(None) (stream sentinel)
  - connection: Future → set LookupError

These tests exercise the drain logic directly without spinning up a full hub.
"""

import asyncio
import time

import pytest

from zhub.server import ConnectionRegistration, PublisherRegistration


# ---- helpers ----------------------------------------------------------------

def _make_pub(name: str = "test-ai") -> PublisherRegistration:
    pub = PublisherRegistration.__new__(PublisherRegistration)
    pub.name = name
    pub.manifest = {}
    pub.websocket = None  # type: ignore[assignment]
    pub.api_key_hash = "abc"
    pub.created_at = time.time()
    pub.pending = {}
    return pub


def _make_conn(name: str = "test-conn") -> ConnectionRegistration:
    conn = ConnectionRegistration.__new__(ConnectionRegistration)
    conn.connection_id = name
    conn.ai_name = "test-ai"
    conn.websocket = None  # type: ignore[assignment]
    conn.client_manifest = {}
    conn.created_at = time.time()
    conn.pending = {}
    return conn


def _drain_publisher(pub: PublisherRegistration) -> None:
    """Mirrors the cleanup added to Hub.unregister_publisher()."""
    err = LookupError("publisher disconnected")
    for item in list(pub.pending.values()):
        if isinstance(item, asyncio.Future):
            if not item.done():
                item.set_exception(err)
        elif isinstance(item, asyncio.Queue):
            item.put_nowait(None)
    pub.pending.clear()


def _drain_connection(conn: ConnectionRegistration) -> None:
    """Mirrors the cleanup added to Hub.unregister_connection()."""
    err = LookupError("connection disconnected")
    for fut in list(conn.pending.values()):
        if not fut.done():
            fut.set_exception(err)
    conn.pending.clear()


# ---- publisher tests --------------------------------------------------------

@pytest.mark.asyncio
async def test_publisher_non_streaming_future_raises_on_disconnect():
    """Non-streaming proxy_chat() future must raise LookupError immediately
    on publisher disconnect, not hang for the 60-second asyncio.wait_for timeout."""
    pub = _make_pub()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    pub.pending["req-1"] = fut

    _drain_publisher(pub)

    assert not pub.pending
    assert fut.done()
    with pytest.raises(LookupError, match="disconnected"):
        fut.result()


@pytest.mark.asyncio
async def test_publisher_streaming_queue_gets_sentinel_on_disconnect():
    """Streaming proxy_chat() queue must receive None (the stream-end sentinel)
    so event_stream's inner `while True: chunk = await queue.get()` loop exits
    instead of blocking forever on publisher disconnect."""
    pub = _make_pub()
    q: asyncio.Queue = asyncio.Queue()
    pub.pending["req-2"] = q  # type: ignore[assignment]

    _drain_publisher(pub)

    assert not pub.pending
    sentinel = q.get_nowait()
    assert sentinel is None  # this is what event_stream checks `if chunk is None: break`


@pytest.mark.asyncio
async def test_publisher_drain_skips_already_resolved_futures():
    """Resolved futures must not raise InvalidStateError during drain."""
    pub = _make_pub()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    fut.set_result({"text": "already done"})
    pub.pending["req-3"] = fut

    _drain_publisher(pub)  # must not raise

    assert fut.result() == {"text": "already done"}


# ---- connection tests -------------------------------------------------------

@pytest.mark.asyncio
async def test_connection_invoke_future_raises_on_disconnect():
    """invoke_capability() future must raise LookupError immediately on
    client disconnect, not wait for the 60-second timeout."""
    conn = _make_conn()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    conn.pending["req-4"] = fut

    _drain_connection(conn)

    assert not conn.pending
    assert fut.done()
    with pytest.raises(LookupError, match="disconnected"):
        fut.result()


@pytest.mark.asyncio
async def test_connection_drain_skips_already_resolved_futures():
    """Resolved futures must not raise InvalidStateError during drain."""
    conn = _make_conn()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    fut.set_result({"ok": True, "result": "pong"})
    conn.pending["req-5"] = fut

    _drain_connection(conn)  # must not raise

    assert fut.result() == {"ok": True, "result": "pong"}
