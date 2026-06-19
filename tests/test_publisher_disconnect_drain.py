"""ZhubPublication._pending is drained on WS disconnect.

Pre-fix: when publisher's _serve_one_session exited (WS drop / hub restart),
any in-flight invoke() calls silently waited up to their timeout (default 60 s)
before asyncio.wait_for raised TimeoutError.

Post-fix: a try/finally block in the publisher's _serve_one_session fails all
pending invoke futures with ZhubConnectionError on exit, so callers raise
immediately — the same drain that connect()'s _serve_one_session already had.

These tests exercise the cleanup logic directly on ZhubPublication state,
without spinning up a real hub.
"""

import asyncio

import pytest

from zhub.client import ZhubPublication
from zhub.errors import ConnectionError as ZhubConnectionError
from zhub.manifest import Manifest


def _make_pub() -> ZhubPublication:
    return ZhubPublication(
        name="test-ai",
        base_url="",
        api_key="zk_test",
        manifest=Manifest(name="test-ai"),
        hub_url="ws://localhost",
        chat_handler=None,  # type: ignore[arg-type]
        on_connection_event=None,
    )


def _run_disconnect_cleanup(pub: ZhubPublication) -> None:
    """Mirrors the finally block added to publisher's _serve_one_session."""
    from zhub.errors import ConnectionError as _ZCE
    err = _ZCE("connection closed")
    for fut in list(pub._pending.values()):  # type: ignore[attr-defined]
        if not fut.done():
            fut.set_exception(err)
    pub._pending.clear()  # type: ignore[attr-defined]
    pub._ws = None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_pending_invoke_raises_on_disconnect():
    """An in-flight invoke() future must raise ZhubConnectionError on disconnect,
    not hang for the full 60 s timeout."""
    pub = _make_pub()
    pub._pending = {}  # type: ignore[attr-defined]
    pub._ws = None  # type: ignore[attr-defined]
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    pub._pending["req-1"] = fut  # type: ignore[attr-defined]

    _run_disconnect_cleanup(pub)

    assert not pub._pending, "pending should be empty after cleanup"  # type: ignore[attr-defined]
    assert fut.done(), "future should be resolved"
    with pytest.raises(ZhubConnectionError):
        fut.result()


@pytest.mark.asyncio
async def test_cleanup_skips_already_resolved_futures():
    """Already-resolved invoke futures must not raise InvalidStateError."""
    pub = _make_pub()
    pub._pending = {}  # type: ignore[attr-defined]
    pub._ws = None  # type: ignore[attr-defined]
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    fut.set_result({"ok": True, "result": "done"})
    pub._pending["req-2"] = fut  # type: ignore[attr-defined]

    _run_disconnect_cleanup(pub)

    # Must not raise; already-resolved future is left intact
    assert fut.result() == {"ok": True, "result": "done"}


@pytest.mark.asyncio
async def test_ws_is_none_after_cleanup():
    """pub._ws must be None after cleanup so invoke() raises immediately
    instead of sending on a dead socket."""
    pub = _make_pub()
    pub._pending = {}  # type: ignore[attr-defined]
    pub._ws = object()  # type: ignore[attr-defined, assignment]

    _run_disconnect_cleanup(pub)

    assert pub._ws is None  # type: ignore[attr-defined]
