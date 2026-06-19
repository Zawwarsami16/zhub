"""End-to-end test: spin up the hub, publisher, and client; verify the full
chat-and-invoke flow.

Skipped if fastapi/uvicorn isn't installed (the server module needs them).
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
    app = create_app()

    def run():
        import uvicorn
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        asyncio.run(server.serve())

    t = threading.Thread(target=run, daemon=True)
    t.start()

    # wait for the server to come up
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.skip("hub did not start in time")

    yield port


@pytest.mark.asyncio
async def test_publish_and_chat(hub_port):
    hub = f"ws://127.0.0.1:{hub_port}"

    def chat_handler(messages, options):
        last = next((m["content"] for m in reversed(messages)
                    if m.get("role") == "user"), "")
        return f"got: {last}"

    pub = publish(
        name="testai",
        description="test",
        chat_handler=chat_handler,
        hub_url=hub,
    )
    # wait for registration
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key, "publisher never registered"

    # client connects
    conn = connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url=hub,
        capabilities={},  # no capabilities exposed
    )
    for _ in range(50):
        if conn._ws is not None:
            break
        await asyncio.sleep(0.05)

    resp = await conn.chat(messages=[{"role": "user", "content": "hello"}])
    assert "got: hello" in resp.get("text", "")


@pytest.mark.asyncio
async def test_invoke_capability(hub_port):
    hub = f"ws://127.0.0.1:{hub_port}"
    received_args = {}

    def chat_handler(messages, options):
        return "ok"

    def fake_capability(args):
        received_args.update(args)
        return {"ok": True, "received": dict(args)}

    pub = publish(
        name="invoker",
        description="test invoker",
        chat_handler=chat_handler,
        hub_url=hub,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url=hub,
        capabilities={
            "test_op": ({"type": "object"}, fake_capability),
        },
    )
    # poll until the publisher receives the connection-event carrying the
    # capability (WS up + hub round-trip + publisher WS message processed)
    cid = None
    for _ in range(60):
        cid = pub.find_capability("test_op")
        if cid is not None:
            break
        await asyncio.sleep(0.05)

    assert cid is not None, "publisher never saw the capability"

    result = await pub.invoke(cid, "test_op", {"hello": "world"})
    assert result.get("ok") is True
    assert received_args.get("hello") == "world"
