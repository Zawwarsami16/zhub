"""Two hubs in-process; one peers the other; /registry/global aggregates."""

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
from zhub import publish


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _start_hub(port: int, peers_env: str = "") -> None:
    """Start a hub in this thread. Reads peers from process env at start."""
    if peers_env:
        os.environ["ZHUB_PEERS"] = peers_env
    else:
        os.environ.pop("ZHUB_PEERS", None)
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
async def test_global_registry_aggregates_peer():
    """Hub A peers hub B. A publisher registered on B is visible via A's
    /registry/global endpoint, annotated with origin."""
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    port_a = _free_port()
    port_b = _free_port()

    # Start B first (no peers), then A peering B.
    threading.Thread(target=_start_hub, args=(port_b, ""), daemon=True).start()
    _wait(port_b)
    threading.Thread(target=_start_hub, args=(port_a, f"http://127.0.0.1:{port_b}"), daemon=True).start()
    _wait(port_a)

    # Publish on B
    pub = publish(
        name="onb",
        description="lives on B",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{port_b}",
        public=True,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key

    # Fetch hub A's global registry
    async with httpx.AsyncClient() as c:
        resp = await c.get(f"http://127.0.0.1:{port_a}/registry/global")
    assert resp.status_code == 200
    data = resp.json()

    names = {e["name"] for e in data}
    assert "onb" in names, f"expected 'onb' in {names}"
    origins = {e.get("origin") for e in data if e["name"] == "onb"}
    assert any(o and o != "self" for o in origins), \
        f"expected at least one non-self origin, got {origins}"


@pytest.mark.asyncio
async def test_global_registry_without_peers_returns_only_local():
    """A hub with no peers configured returns only its own local listings
    from /registry/global (degenerate but correct)."""
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    port = _free_port()
    threading.Thread(target=_start_hub, args=(port, ""), daemon=True).start()
    _wait(port)

    pub = publish(
        name="solo",
        description="no peers",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{port}",
        public=True,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient() as c:
        resp = await c.get(f"http://127.0.0.1:{port}/registry/global")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert all(e.get("origin") == "self" for e in data)


@pytest.mark.asyncio
async def test_global_registry_offline_peer_omitted_gracefully():
    """If a peer is unreachable, /registry/global still returns local
    listings — the offline peer is silently skipped."""
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    bogus_port = _free_port()  # nothing listens here
    port = _free_port()
    threading.Thread(target=_start_hub,
                     args=(port, f"http://127.0.0.1:{bogus_port}"),
                     daemon=True).start()
    _wait(port)

    async with httpx.AsyncClient() as c:
        resp = await c.get(f"http://127.0.0.1:{port}/registry/global")
    assert resp.status_code == 200  # not 502
    # Body may be empty if no local publishers — that's fine.
