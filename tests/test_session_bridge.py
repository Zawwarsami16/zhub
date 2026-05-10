"""Smoke test for examples/session_bridge_publisher.py — proves the
file-bridge round-trips a chat request to whatever the watcher writes.
Uses the example's _example_auto_watcher to avoid needing a human."""

import asyncio
import os
import socket
import sys
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

# Import the example's helpers — it lives in examples/ which isn't a package
# so we go through the file directly via importlib.
import importlib.util
import pathlib

_EXAMPLES_PATH = pathlib.Path(__file__).parent.parent / "examples" / "session_bridge_publisher.py"
spec = importlib.util.spec_from_file_location("session_bridge_publisher", _EXAMPLES_PATH)
sbp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sbp)  # type: ignore


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_file_bridge_round_trips(tmp_path):
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")

    port = _free_port()
    inbox = str(tmp_path / "inbox")
    outbox = str(tmp_path / "outbox")
    os.makedirs(inbox)
    os.makedirs(outbox)

    app = create_app()

    def serve():
        config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                log_level="warning")
        asyncio.run(uvicorn.Server(config).serve())

    threading.Thread(target=serve, daemon=True).start()
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)

    handler = sbp.make_chat_handler(inbox, outbox, timeout_s=10)
    pub = publish(
        name="bridge-bot",
        description="bridge smoke test",
        chat_handler=handler,
        hub_url=f"ws://127.0.0.1:{port}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    # spawn the auto-replier as a background task so chats land + get replies
    watcher = asyncio.create_task(
        sbp._example_auto_watcher(inbox, outbox, reply_text="bridged")
    )

    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            resp = await c.post(
                f"http://127.0.0.1:{port}/{pub.name}/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "ping"}]},
                headers={"Authorization": f"Bearer {pub.api_key}"},
            )
        assert resp.status_code == 200, resp.text
        text = resp.json()["choices"][0]["message"]["content"]
        assert "bridged" in text
        assert "ping" in text
    finally:
        watcher.cancel()
