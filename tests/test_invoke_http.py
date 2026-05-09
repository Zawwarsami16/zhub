"""Phase 2.3 — Public HTTP /v1/invoke endpoint.

Today, invoking a connected client's capability requires going through the
bidirectional WS channel as a publisher. We add a Bearer-authed HTTP
endpoint so external clients (e.g. zhub.mcp_server, scripts) can call any
connected capability directly without owning the WS session.

POST /<ai>/v1/invoke
  Authorization: Bearer <publisher api_key>
  body: { capability: str, args: dict, connection_id?: str }
  →
  { ok: bool, result: any, connection_id: str }    on success
  401 if api_key mismatch, 404 if capability not connected
"""

import asyncio
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


@pytest.fixture(scope="module")
def invoke_hub_port():
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    port = _free_port()
    app = create_app()

    def run():
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
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
async def test_invoke_capability_via_http(invoke_hub_port):
    """POST /<ai>/v1/invoke with valid api_key calls the connected
    client's capability and returns the unwrapped result."""
    hub_ws = f"ws://127.0.0.1:{invoke_hub_port}"
    hub_http = f"http://127.0.0.1:{invoke_hub_port}"
    captured = {}

    pub = publish(
        name="invoke-bot",
        description="invoke test",
        chat_handler=lambda m, o: "ok",
        hub_url=hub_ws,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    def get_battery(args):
        captured.update(args)
        return {"level": 78, "charging": False}

    conn = connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url=hub_ws,
        capabilities={"get_battery": ({"type": "object"}, get_battery)},
    )
    await asyncio.sleep(0.6)

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{hub_http}/{pub.name}/v1/invoke",
            json={"capability": "get_battery", "args": {"include_temp": True}},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["result"] == {"level": 78, "charging": False}
    assert body.get("connection_id", "").startswith("cx_")
    assert captured == {"include_temp": True}


@pytest.mark.asyncio
async def test_invoke_rejects_bad_api_key(invoke_hub_port):
    hub_ws = f"ws://127.0.0.1:{invoke_hub_port}"
    hub_http = f"http://127.0.0.1:{invoke_hub_port}"

    pub = publish(
        name="invoke-auth-bot",
        description="auth test",
        chat_handler=lambda m, o: "ok",
        hub_url=hub_ws,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    conn = connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url=hub_ws,
        capabilities={"x": ({"type": "object"}, lambda a: 1)},
    )
    await asyncio.sleep(0.5)

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{hub_http}/{pub.name}/v1/invoke",
            json={"capability": "x", "args": {}},
            headers={"Authorization": "Bearer zk_wrong"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_invoke_404_when_capability_not_connected(invoke_hub_port):
    hub_ws = f"ws://127.0.0.1:{invoke_hub_port}"
    hub_http = f"http://127.0.0.1:{invoke_hub_port}"

    pub = publish(
        name="invoke-missing-bot",
        description="missing cap",
        chat_handler=lambda m, o: "ok",
        hub_url=hub_ws,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{hub_http}/{pub.name}/v1/invoke",
            json={"capability": "nope", "args": {}},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 404
