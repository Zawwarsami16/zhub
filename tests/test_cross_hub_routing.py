"""Phase 1.1 — Cross-hub call routing.

Hub A receives a chat request for an AI registered on hub B. Hub A proxies
through to B via HTTP, returns the response. Real federation — not just
/registry/global discovery.
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
from zhub import publish


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
async def test_cross_hub_chat_proxies_from_a_to_b():
    """Publish AI 'bob' on hub B. POST /bob/v1/chat/completions to hub A.
    Expect A to proxy through to B and return B's response."""
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    port_a = _free_port()
    port_b = _free_port()

    # Start B first (no peers). Then A peering B.
    threading.Thread(target=_start_hub, args=(port_b, "", "hub-b"), daemon=True).start()
    _wait(port_b)
    threading.Thread(
        target=_start_hub,
        args=(port_a, f"http://127.0.0.1:{port_b}", "hub-a"),
        daemon=True,
    ).start()
    _wait(port_a)

    pub = publish(
        name="bob",
        description="lives on B",
        chat_handler=lambda messages, opts: "from-b",
        hub_url=f"ws://127.0.0.1:{port_b}",
        public=True,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key, "publisher never registered on B"

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"http://127.0.0.1:{port_a}/bob/v1/chat/completions",
            json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "from-b"
    # Origin header annotates which peer served the request
    assert resp.headers.get("x-zhub-origin", "").startswith("http://127.0.0.1:"), (
        f"missing or bad x-zhub-origin: {resp.headers}"
    )


@pytest.mark.asyncio
async def test_cross_hub_404_when_no_peer_has_ai():
    """Two peered hubs, AI registered on neither. Expect 404."""
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

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"http://127.0.0.1:{port_a}/ghost/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": "Bearer zk_anything"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cross_hub_loop_detected_returns_508():
    """If X-Zhub-Forwarded-By already contains our hub_id, refuse with 508."""
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    port = _free_port()
    threading.Thread(target=_start_hub, args=(port, "", "hub-x"), daemon=True).start()
    _wait(port)

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"http://127.0.0.1:{port}/anybody/v1/chat/completions",
            json={"messages": []},
            headers={
                "Authorization": "Bearer zk_x",
                # hub-x is already in the forwarding chain — looping back.
                "X-Zhub-Forwarded-By": "hub-x",
            },
        )
    assert resp.status_code == 508, f"expected 508 loop, got {resp.status_code}"


@pytest.mark.asyncio
async def test_cross_hub_local_takes_precedence_over_peer():
    """If an AI name exists locally AND on a peer, local serves it."""
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

    pub_a = publish(
        name="twin",
        description="local twin",
        chat_handler=lambda m, o: "from-a",
        hub_url=f"ws://127.0.0.1:{port_a}",
        public=True,
    )
    pub_b = publish(
        name="twin",
        description="remote twin",
        chat_handler=lambda m, o: "from-b",
        hub_url=f"ws://127.0.0.1:{port_b}",
        public=True,
    )
    for _ in range(50):
        if pub_a.api_key and pub_b.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"http://127.0.0.1:{port_a}/twin/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {pub_a.api_key}"},
        )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "from-a"
