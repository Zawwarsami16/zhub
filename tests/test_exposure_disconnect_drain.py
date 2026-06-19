"""ExposureRegistration.pending is drained on WS disconnect.

Pre-fix: unregister_exposure() popped the exposure from hub.exposures but
did not fail pending invoke_exposure() futures. An in-flight
`POST /exposures/<id>/invoke` silently waited the full 60-second timeout
before asyncio.wait_for raised TimeoutError, returning 504 instead of 404.

Post-fix: unregister_exposure() captures the ExposureRegistration before
popping it, then sets LookupError("exposure disconnected") on every unresolved
pending future. The invoke_exposure_http handler's existing
`except LookupError` catch then converts it to 404 — correctly describing
the situation (exposure is gone, not merely slow).
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from zhub.server import ExposureRegistration


def _make_exp() -> ExposureRegistration:
    exp = ExposureRegistration.__new__(ExposureRegistration)
    exp.exposure_id = "ex_test"
    exp.name = "test-device"
    exp.websocket = None  # type: ignore[assignment]
    exp.manifest = {"capabilities": [{"name": "ping"}]}
    exp.device_key_hash = "abc"
    exp.pending = {}
    import time
    exp.created_at = time.time()
    return exp


def _run_drain(exp: ExposureRegistration) -> None:
    """Mirrors the cleanup added to unregister_exposure."""
    err = LookupError("exposure disconnected")
    for fut in list(exp.pending.values()):
        if not fut.done():
            fut.set_exception(err)
    exp.pending.clear()


@pytest.mark.asyncio
async def test_pending_future_raises_on_disconnect():
    """An in-flight invoke future must raise LookupError immediately on
    exposure disconnect, not hang for the full 60-second timeout."""
    exp = _make_exp()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    exp.pending["req-1"] = fut

    _run_drain(exp)

    assert not exp.pending, "pending dict should be empty after drain"
    assert fut.done(), "future must be resolved (not hanging)"
    with pytest.raises(LookupError, match="disconnected"):
        fut.result()


@pytest.mark.asyncio
async def test_cleanup_skips_already_resolved_futures():
    """Already-resolved futures must not raise InvalidStateError."""
    exp = _make_exp()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    fut.set_result({"ok": True, "result": "pong"})
    exp.pending["req-2"] = fut

    _run_drain(exp)  # must not raise

    assert fut.result() == {"ok": True, "result": "pong"}


@pytest.mark.asyncio
async def test_pending_is_empty_after_drain():
    """All entries cleared regardless of how many pending futures existed."""
    exp = _make_exp()
    loop = asyncio.get_running_loop()
    for i in range(3):
        exp.pending[f"req-{i}"] = loop.create_future()

    _run_drain(exp)

    assert len(exp.pending) == 0
