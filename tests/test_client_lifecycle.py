"""ZhubPublication / ZhubConnection / ZhubExposure lifecycle API.

Pre-fix: run_forever() was missing on all three classes.  stop() was missing
on ZhubConnection and ZhubExposure.  The module quickstart and publish()
docstring both referenced pub.run_forever() / conn.run_forever() — following
either caused AttributeError before any connection attempt.

Post-fix: all three classes expose stop() + run_forever().  run_forever()
awaits the stop event so it returns as soon as stop() is called from a
concurrent task, making it safe to use as the final await in asyncio.run(main()).
"""

import asyncio

import pytest

from zhub import ZhubConnection, ZhubExposure, ZhubPublication
from zhub.client import ZhubConnection, ZhubExposure
from zhub.manifest import Manifest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pub() -> ZhubPublication:
    from zhub.client import publish
    from zhub.manifest import chat_only_manifest
    from dataclasses import fields
    # Build a bare ZhubPublication without starting the background task.
    from zhub.client import ZhubPublication
    import dataclasses
    pub = object.__new__(ZhubPublication)
    pub.name = "testpub"
    pub.base_url = ""
    pub.api_key = ""
    pub.manifest = Manifest(name="testpub")
    pub.hub_url = "ws://localhost"
    pub.chat_handler = lambda msgs, opts: "hi"
    pub.on_connection_event = None
    pub._task = None
    pub._stop_event = asyncio.Event()
    pub._connections = {}
    pub._pending = {}
    pub._ws = None
    return pub


def _make_conn() -> ZhubConnection:
    return ZhubConnection(
        ai_name="ai",
        api_key="zk_test",
        hub_url="ws://localhost",
        client_manifest=Manifest(name="ai-client"),
        capabilities={},
    )


def _make_exp() -> ZhubExposure:
    return ZhubExposure(
        name="dev",
        hub_url="ws://localhost",
        client_manifest=Manifest(name="dev"),
        capabilities={},
    )


# ---------------------------------------------------------------------------
# run_forever() tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pub_run_forever_returns_when_stop_called():
    """run_forever() must return (not hang) after stop() is called."""
    pub = _make_pub()
    task = asyncio.create_task(pub.run_forever())
    await asyncio.sleep(0)  # yield so run_forever() enters wait
    assert not task.done(), "run_forever() should still be blocking"
    await pub.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()


@pytest.mark.asyncio
async def test_conn_run_forever_returns_when_stop_called():
    """ZhubConnection.run_forever() must return after stop()."""
    conn = _make_conn()
    task = asyncio.create_task(conn.run_forever())
    await asyncio.sleep(0)
    assert not task.done()
    await conn.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()


@pytest.mark.asyncio
async def test_exp_run_forever_returns_when_stop_called():
    """ZhubExposure.run_forever() must return after stop()."""
    exp = _make_exp()
    task = asyncio.create_task(exp.run_forever())
    await asyncio.sleep(0)
    assert not task.done()
    await exp.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()


# ---------------------------------------------------------------------------
# stop() sets the stop event and cancels any background task
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_conn_stop_sets_event():
    conn = _make_conn()
    assert not conn._stop_event.is_set()
    await conn.stop()
    assert conn._stop_event.is_set()


@pytest.mark.asyncio
async def test_exp_stop_sets_event():
    exp = _make_exp()
    assert not exp._stop_event.is_set()
    await exp.stop()
    assert exp._stop_event.is_set()


@pytest.mark.asyncio
async def test_conn_stop_cancels_task():
    conn = _make_conn()
    # Attach a real long-running dummy task
    dummy = asyncio.create_task(asyncio.sleep(3600))
    conn._task = dummy
    await conn.stop()
    # Give the event loop a tick to process the cancellation
    await asyncio.sleep(0)
    assert dummy.cancelled()


@pytest.mark.asyncio
async def test_exp_stop_cancels_task():
    exp = _make_exp()
    dummy = asyncio.create_task(asyncio.sleep(3600))
    exp._task = dummy
    await exp.stop()
    await asyncio.sleep(0)
    assert dummy.cancelled()
