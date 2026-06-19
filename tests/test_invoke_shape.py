"""Regression test — invoke result must be single-wrapped, not double-wrapped.

Earlier the hub re-wrapped the connection's invoke-result envelope payload
into another invoke-result, producing nested {ok:true, result:{ok, result, error}}.
The fix: hub forwards the connection payload as-is.
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
async def test_invoke_result_is_single_wrapped(hub_port):
    """Invoke result returned to publisher should be {ok, result, error},
    NOT {ok, result: {ok, result, error}, error}."""
    hub_url = f"ws://127.0.0.1:{hub_port}"

    pub = publish(
        name="invwrap",
        description="invoke wrapping test",
        chat_handler=lambda m, o: "ok",
        hub_url=hub_url,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    def cap_handler(args):
        # Returns a structured result the AI should see directly
        return {"delivered": True, "to": args.get("to"), "msg_id": "wa_123"}

    conn = connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url=hub_url,
        capabilities={"send": ({"type": "object"}, cap_handler)},
    )
    for _ in range(60):
        if pub.find_capability("send") is not None:
            break
        await asyncio.sleep(0.1)
    assert pub.find_capability("send") is not None, "connection never established"

    cid = pub.find_capability("send")
    assert cid is not None
    result = await pub.invoke(cid, "send", {"to": "Ammi"})

    # Single-wrap shape: {ok: true, result: <handler return>, error: None}
    assert result["ok"] is True
    inner = result["result"]
    # The handler returned a flat dict — not another {ok, result, error}
    assert "ok" not in inner or inner.get("delivered") is True
    assert inner.get("to") == "Ammi"
    assert inner.get("msg_id") == "wa_123"
