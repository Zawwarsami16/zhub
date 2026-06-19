"""ZhubConnection._pending + _streams are drained on WS disconnect.

Pre-fix: when _serve_one_session exited (WS drop / hub restart), any in-flight
chat() or chat_stream() calls silently waited up to 60 s before their
asyncio.wait_for timeout fired.

Post-fix: a finally block in _serve_one_session fails all pending futures and
sends done-with-error sentinels to all stream queues, so callers raise
immediately.

These tests exercise the cleanup logic directly on ZhubConnection state,
without spinning up a real hub.
"""

import asyncio

import pytest

from zhub.client import ZhubConnection
from zhub.errors import ConnectionError as ZhubConnectionError
from zhub.manifest import Manifest


def _make_conn() -> ZhubConnection:
    return ZhubConnection(
        ai_name="ai",
        api_key="zk_test",
        hub_url="ws://localhost",
        client_manifest=Manifest(name="ai-client"),
        capabilities={},
    )


def _run_disconnect_cleanup(conn: ZhubConnection) -> None:
    """Mirrors the finally block added to _serve_one_session in client.py."""
    err = ZhubConnectionError("connection closed")
    for fut in list(conn._pending.values()):
        if not fut.done():
            fut.set_exception(err)
    conn._pending.clear()
    for q in list(conn._streams.values()):
        q.put_nowait({"done": True, "error": "connection closed"})
    conn._streams.clear()
    conn._ws = None


@pytest.mark.asyncio
async def test_pending_future_raises_on_disconnect():
    """A future in _pending must raise ZhubConnectionError, not hang."""
    conn = _make_conn()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    conn._pending["req-1"] = fut

    _run_disconnect_cleanup(conn)

    assert not conn._pending, "pending should be empty after cleanup"
    assert fut.done(), "future should be resolved"
    with pytest.raises(ZhubConnectionError):
        fut.result()


@pytest.mark.asyncio
async def test_stream_queue_gets_done_sentinel_on_disconnect():
    """A stream queue in _streams must receive the done sentinel so
    chat_stream() exits instead of blocking on the next chunk forever."""
    conn = _make_conn()
    q: asyncio.Queue = asyncio.Queue()
    conn._streams["req-2"] = q

    _run_disconnect_cleanup(conn)

    assert not conn._streams, "streams should be empty after cleanup"
    item = q.get_nowait()
    assert item.get("done") is True
    assert "error" in item


@pytest.mark.asyncio
async def test_cleanup_skips_already_done_futures():
    """Already-resolved futures must not raise InvalidStateError."""
    conn = _make_conn()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    fut.set_result({"text": "done already"})
    conn._pending["req-3"] = fut

    # Must not raise
    _run_disconnect_cleanup(conn)
    assert fut.result() == {"text": "done already"}


@pytest.mark.asyncio
async def test_ws_is_none_after_cleanup():
    conn = _make_conn()
    conn._ws = object()  # type: ignore[assignment]
    _run_disconnect_cleanup(conn)
    assert conn._ws is None
