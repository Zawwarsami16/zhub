"""End-to-end streaming test — yield chunks from publisher, receive on client side."""

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
def hub_port_streaming():
    if not SERVER_AVAILABLE:
        pytest.skip("fastapi/uvicorn not installed")
    port = _free_port()
    app = create_app()

    def run():
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        asyncio.run(server.serve())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.skip("hub did not start")
    yield port


@pytest.mark.asyncio
async def test_streaming_publisher_yields_chunks(hub_port_streaming):
    hub = f"ws://127.0.0.1:{hub_port_streaming}"

    def streaming_handler(messages, options):
        # Generator that yields word-by-word
        for word in ["the ", "quick ", "brown ", "fox"]:
            yield word

    pub = publish(
        name="streamer",
        description="streaming test publisher",
        chat_handler=streaming_handler,
        hub_url=hub,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key

    conn = connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url=hub,
        capabilities={},
    )
    await asyncio.sleep(0.5)

    chunks = []
    async for chunk in conn.chat_stream(messages=[{"role": "user", "content": "hi"}]):
        chunks.append(chunk)

    full_text = "".join(chunks)
    assert "the" in full_text
    assert "fox" in full_text
    assert len(chunks) >= 3  # got actual chunks, not a single coalesced response
