"""Phase 1.1b — Cross-hub WebSocket routing.

A connect()-side client opens its WS against hub A. The AI is registered on
hub B (a peer of A). Hub A transparently tunnels the WebSocket through to
B's /ws/connect — the client doesn't need to know federation happened.
Bidirectional: chat works one way, invoke-request flows the other.

Loop prevention: register-connection payload carries a ``via`` chain of
hub_ids; if our hub_id is already in the chain we refuse rather than
forwarding back.
"""

import asyncio
import os
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


def _start_hub(port: int, peers_env: str = "", hub_id: str = "") -> None:
    if peers_env:
        os.environ["ZHUB_PEERS"] = peers_env
    else:
        os.environ.pop("ZHUB_PEERS", None)
    if hub_id:
        os.environ["ZHUB_HUB_ID"] = hub_id
    else:
        os.environ.pop("ZHUB_HUB_ID", None)
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    asyncio.run(uvicorn.Server(config).serve())


def _wait(port: int) -> None:
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.1)


@pytest.mark.asyncio
async def test_connect_via_peer_hub_chat_round_trip():
    """Publish AI on B. Client connects via A's /ws/connect. chat() through
    the connection should reach the publisher on B and come back."""
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    port_a = _free_port()
    port_b = _free_port()

    threading.Thread(target=_start_hub, args=(port_b, "", "hub-b"), daemon=True).start()
    _wait(port_b)
    threading.Thread(
        target=_start_hub,
        args=(port_a, f"http://127.0.0.1:{port_b}", "hub-a"),
        daemon=True,
    ).start()
    _wait(port_a)

    pub = publish(
        name="remote-bot",
        description="lives on B",
        chat_handler=lambda m, o: f"hello from B (saw {len(m)} msgs)",
        hub_url=f"ws://127.0.0.1:{port_b}",
        public=True,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key

    # Client connects via A — A should tunnel to B
    conn = connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url=f"ws://127.0.0.1:{port_a}",
        capabilities={},
    )
    # Wait for tunnel to be set up + register-connection acknowledged
    for _ in range(50):
        if conn._ws is not None:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.5)

    resp = await conn.chat(messages=[{"role": "user", "content": "ping"}], timeout=5.0)
    text = resp.get("text", "")
    assert "hello from B" in text, f"expected B's response, got {text!r}"


@pytest.mark.asyncio
async def test_connect_via_unknown_ai_errors_quickly():
    """Client connects to A asking for an AI nobody knows. A should send a
    register_failed error and close — not hang."""
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    port_a = _free_port()
    port_b = _free_port()

    threading.Thread(target=_start_hub, args=(port_b, "", "hub-b"), daemon=True).start()
    _wait(port_b)
    threading.Thread(
        target=_start_hub,
        args=(port_a, f"http://127.0.0.1:{port_b}", "hub-a"),
        daemon=True,
    ).start()
    _wait(port_a)

    import websockets
    from zhub.protocol import register_connection, Envelope

    async with websockets.connect(f"ws://127.0.0.1:{port_a}/ws/connect") as ws:
        await ws.send(register_connection("ghost", "zk_nope", {"name": "x"}).to_json())
        # Should receive an error envelope within a couple seconds
        raw = await asyncio.wait_for(ws.recv(), timeout=4.0)
        env = Envelope.from_json(raw)
        assert env.type == "error", f"expected error envelope, got {env.type}: {env.payload!r}"
        assert env.payload.get("code") == "register_failed"
