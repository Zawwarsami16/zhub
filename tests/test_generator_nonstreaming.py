"""Regression test — when a publisher's chat handler is a generator BUT the
caller did not request streaming, the publisher must accumulate all yields
and emit a single chat-response. Otherwise non-streaming HTTP/openai-py
callers time out (504 from hub).
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

    t = threading.Thread(target=run, daemon=True)
    t.start()
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)
    yield port


@pytest.mark.asyncio
async def test_generator_handler_serves_nonstreaming_callers(hub_port):
    """A publisher with a generator handler must answer non-streaming chat
    requests by accumulating all yields into a single response."""
    hub_url = f"ws://127.0.0.1:{hub_port}"

    def gen_handler(messages, options):
        for word in ["a", "b", "c"]:
            yield word

    pub = publish(
        name="gen-nostream",
        description="gen vs non-stream",
        chat_handler=gen_handler,
        hub_url=hub_url,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    conn = connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url=hub_url,
        capabilities={},
    )
    for _ in range(60):
        if conn._ws is not None:
            break
        await asyncio.sleep(0.1)
    assert conn._ws is not None, "connection never established"

    # Non-streaming chat — must complete (not time out) and return concatenated text
    resp = await asyncio.wait_for(
        conn.chat(messages=[{"role": "user", "content": "x"}]),
        timeout=10.0,
    )
    assert resp.get("text") == "abc"
