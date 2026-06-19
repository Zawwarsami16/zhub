"""End-to-end council pattern — coordinator AI orchestrates 3 panel AIs through the hub."""

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
from zhub import publish, connect


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def hub_port():
    if not SERVER_AVAILABLE:
        pytest.skip("fastapi/uvicorn not installed")
    port = _free_port()

    def run():
        config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
        asyncio.run(uvicorn.Server(config).serve())

    threading.Thread(target=run, daemon=True).start()
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)
    yield port


@pytest.mark.asyncio
async def test_coordinator_calls_all_panel_members(hub_port):
    """The coordinator AI publishes itself, also connects to three other
    published AIs, and synthesizes their replies."""
    hub_url = f"ws://127.0.0.1:{hub_port}"

    # Three "panel" AIs — each returns a distinct signature.
    pubs = []
    for name, signature in [("alpha", "[A]"), ("beta", "[B]"), ("gamma", "[C]")]:
        p = publish(
            name=name,
            description=f"panel member {name}",
            chat_handler=(lambda sig: (lambda m, o: f"{sig} {m[-1]['content']}"))(signature),
            hub_url=hub_url,
        )
        pubs.append(p)

    for p in pubs:
        for _ in range(50):
            if p.api_key:
                break
            await asyncio.sleep(0.05)
        assert p.api_key, f"panel member {p.name} never registered"

    # Coordinator: publishes a chat handler that internally connects to each
    # panel member and aggregates.
    panel_creds = [(p.name, p.api_key) for p in pubs]

    async def coordinator_handler(messages, options):
        question = messages[-1]["content"]
        replies = []
        for name, key in panel_creds:
            sub = connect(
                ai_name=name, api_key=key, hub_url=hub_url,
                capabilities={},
            )
            for _ in range(50):
                if sub._ws is not None:
                    break
                await asyncio.sleep(0.05)
            r = await sub.chat(messages=[{"role": "user", "content": question}])
            replies.append(r.get("text", ""))
        return "council: " + " | ".join(replies)

    coord = publish(
        name="coordinator",
        description="multi-AI council",
        chat_handler=coordinator_handler,
        hub_url=hub_url,
    )
    for _ in range(50):
        if coord.api_key:
            break
        await asyncio.sleep(0.05)
    assert coord.api_key, "coordinator never registered"

    # Connect a client that asks the coordinator a question.
    client = connect(
        ai_name=coord.name, api_key=coord.api_key, hub_url=hub_url,
        capabilities={},
    )
    for _ in range(50):
        if client._ws is not None:
            break
        await asyncio.sleep(0.05)

    resp = await asyncio.wait_for(
        client.chat(messages=[{"role": "user", "content": "ping"}]),
        timeout=15.0,
    )
    text = resp.get("text", "")
    assert text.startswith("council:"), f"expected council prefix, got: {text!r}"
    assert "[A] ping" in text, f"missing alpha signature: {text!r}"
    assert "[B] ping" in text, f"missing beta signature: {text!r}"
    assert "[C] ping" in text, f"missing gamma signature: {text!r}"
