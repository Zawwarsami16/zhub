"""Regression test: unregister_publisher must drain hub.client_routes.

When a ws_connect client sends a chat-request that the hub routes to a
publisher (stored in hub.client_routes) and the publisher then disconnects
before responding, the prior behaviour was:
  - hub.client_routes[request_id] lingered forever (entry leak)
  - the ws_connect client's WebSocket received no error response
  - the ws_connect client hung indefinitely waiting for the AI's reply

Fix: unregister_publisher() iterates client_routes, finds every entry whose
ai_name matches the departing publisher, sends an error_envelope back to the
waiting client WebSocket, and removes the stale entries.
"""

import asyncio
import json
import time

import pytest

try:
    import fastapi  # noqa
    import uvicorn  # noqa
    DEPS_AVAILABLE = True
except ImportError:
    DEPS_AVAILABLE = False

if DEPS_AVAILABLE:
    from zhub.server import Hub, PublisherRegistration


def _make_pub(name: str = "test-ai") -> "PublisherRegistration":
    pub = PublisherRegistration.__new__(PublisherRegistration)
    pub.name = name
    pub.manifest = {}
    pub.websocket = None  # type: ignore[assignment]
    pub.api_key_hash = "deadbeef"
    pub.created_at = time.time()
    pub.pending = {}
    return pub


class _FakeWS:
    """Minimal websocket stub that captures sent text."""
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


@pytest.mark.skipif(not DEPS_AVAILABLE, reason="fastapi not installed")
@pytest.mark.asyncio
async def test_client_routes_drained_on_publisher_disconnect():
    """Stale client_routes entry for a departing publisher must be removed and
    the waiting ws_connect client must receive an error envelope immediately,
    not hang until its connection times out or is manually closed."""
    hub = Hub()
    pub = _make_pub("away-ai")

    # Plant the publisher in the hub's in-memory registry (no WS — we only
    # need the cleanup path; the send path is covered separately).
    hub.publishers["away-ai"] = pub
    hub.api_keys["zk_test"] = "away-ai"

    # Simulate a ws_connect client that sent a chat-request routed to away-ai.
    fake_ws = _FakeWS()
    hub.client_routes["req-1"] = (fake_ws, "away-ai")  # type: ignore[assignment]

    # Also add an unrelated route for a different AI — must survive.
    other_ws = _FakeWS()
    hub.client_routes["req-2"] = (other_ws, "other-ai")  # type: ignore[assignment]

    # Publisher disconnects.
    await hub.unregister_publisher("away-ai")

    # The stale route must be gone.
    assert "req-1" not in hub.client_routes, "stale client_route was not removed"

    # The unrelated route must be untouched.
    assert "req-2" in hub.client_routes, "unrelated client_route was wrongly removed"

    # The waiting client must have received an error envelope.
    assert fake_ws.sent, "no error sent to waiting ws_connect client"
    msg = json.loads(fake_ws.sent[0])
    assert msg["type"] == "error"
    assert msg["request_id"] == "req-1"
    assert msg["payload"]["code"] == "ai_offline"

    # The other client must not have been touched.
    assert not other_ws.sent


@pytest.mark.skipif(not DEPS_AVAILABLE, reason="fastapi not installed")
@pytest.mark.asyncio
async def test_client_routes_drain_tolerates_broken_ws():
    """If the waiting client's WS has already closed (send_text raises), the
    drain must still remove the entry and must not propagate the exception."""
    hub = Hub()
    pub = _make_pub("crash-ai")
    hub.publishers["crash-ai"] = pub

    class _BrokenWS:
        async def send_text(self, _text: str) -> None:
            raise RuntimeError("connection already closed")

    hub.client_routes["req-x"] = (_BrokenWS(), "crash-ai")  # type: ignore[assignment]

    # Must not raise even though send_text crashes.
    await hub.unregister_publisher("crash-ai")

    assert "req-x" not in hub.client_routes, "stale route not removed despite broken WS"


@pytest.mark.skipif(not DEPS_AVAILABLE, reason="fastapi not installed")
@pytest.mark.asyncio
async def test_client_routes_drain_with_no_routes():
    """Unregistering a publisher that has no client_routes entries must be a
    no-op (no KeyError or AttributeError)."""
    hub = Hub()
    pub = _make_pub("clean-ai")
    hub.publishers["clean-ai"] = pub

    # No client_routes entries at all.
    await hub.unregister_publisher("clean-ai")  # must not raise
    assert "clean-ai" not in hub.publishers
