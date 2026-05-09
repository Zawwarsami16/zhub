"""Auto-reconnect after hub WS drops or hub restarts.

Today (pre-1.5) the publish/connect runners die permanently on disconnect.
This test asserts they re-register automatically when the hub comes back.
"""

import asyncio
import socket
import threading
import time

import pytest

try:
    import fastapi  # noqa
    import uvicorn  # noqa
    SERVER_AVAILABLE = True
except ImportError:
    SERVER_AVAILABLE = False

if SERVER_AVAILABLE:
    from zhub.server import create_app
from zhub import publish


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class _HubThread:
    """Owns a uvicorn server in a daemon thread. Allows clean stop()."""

    def __init__(self, port: int, db_path: str):
        self.port = port
        self.db_path = db_path
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    def start(self):
        config = uvicorn.Config(
            create_app(db_path=self.db_path), host="127.0.0.1", port=self.port,
            log_level="warning",
        )
        self.server = uvicorn.Server(config)

        def _run():
            asyncio.run(self.server.serve())

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()
        # wait for port
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.1)

    def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=5)


@pytest.mark.asyncio
async def test_publisher_auto_reregisters_after_hub_restart():
    """Publisher's WS dies when hub stops. After hub comes back on the same
    port + db, publisher reconnects + re-registers via key pinning."""
    if not SERVER_AVAILABLE:
        pytest.skip("fastapi/uvicorn not installed")

    import tempfile, os
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        port = _free_port()
        hub = _HubThread(port, db_path)
        hub.start()

        # First registration — fresh hub, no prior key.
        pub = publish(
            name="reconn",
            description="reconnect test",
            chat_handler=lambda m, o: "alive",
            hub_url=f"ws://127.0.0.1:{port}",
        )
        for _ in range(50):
            if pub.api_key:
                break
            await asyncio.sleep(0.05)
        first_key = pub.api_key
        assert first_key

        # Stop the hub — publisher's WS will drop.
        hub.stop()
        await asyncio.sleep(0.3)

        # Restart the hub on the same port + same db.
        hub2 = _HubThread(port, db_path)
        hub2.start()

        # Wait long enough for the reconnect backoff to fire (initial 1s + jitter).
        for _ in range(50):
            if pub.api_key == first_key:
                # api_key still set — but now actually serving via hub2?
                # try a chat to confirm reconnection is alive
                pass
            await asyncio.sleep(0.2)

        # Verify by sending a chat through the new hub — only succeeds if
        # publisher has reconnected and re-registered.
        from zhub import connect
        client = connect(
            ai_name="reconn", api_key=first_key,
            hub_url=f"ws://127.0.0.1:{port}",
            capabilities={},
        )
        await asyncio.sleep(0.5)

        try:
            resp = await asyncio.wait_for(
                client.chat(messages=[{"role": "user", "content": "ping"}]),
                timeout=10.0,
            )
            assert resp.get("text") == "alive", \
                f"reconnected publisher didn't serve chat: {resp!r}"
        finally:
            hub2.stop()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass
