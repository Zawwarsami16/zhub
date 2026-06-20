"""Peer-proxy Authorization header regression.

When a client calls hub A without an api_key (anonymous request), and hub A
peer-routes the request to hub B, it must NOT send `Authorization: ""` to
hub B.  Sending an empty-value Authorization header is malformed HTTP and
many proxies / nginx / uvicorn front-ends reject it with 400.

The fix: omit the Authorization header entirely when api_key is empty.
"""

import asyncio
import json
import socket
import threading
import time
from typing import Any

import pytest

try:
    import fastapi
    import httpx
    import uvicorn
    from fastapi import FastAPI, Request
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


def _wait(port: int) -> None:
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.1)


@pytest.mark.asyncio
async def test_peer_proxy_omits_auth_header_when_no_key():
    """Hub A peer-routes to a recording stub; no client api_key →
    Authorization header must be absent (not present with blank value)."""
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")

    received_headers: dict[str, Any] = {}
    stub_port = _free_port()
    hub_port = _free_port()

    # --- tiny recording stub that masquerades as a peer hub ---------------
    stub_app = FastAPI()

    @stub_app.get("/registry")
    async def registry():
        # Advertise a publisher so _find_peer_for finds it here.
        return [{"name": "remote-bot", "description": "on stub", "public": True}]

    @stub_app.post("/remote-bot/v1/chat/completions")
    async def chat(request: Request):
        received_headers.update(dict(request.headers))
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    def run_stub():
        cfg = uvicorn.Config(stub_app, host="127.0.0.1", port=stub_port,
                             log_level="warning")
        asyncio.run(uvicorn.Server(cfg).serve())

    # --- real hub A that peers the stub -----------------------------------
    import os
    env_backup = os.environ.get("ZHUB_PEERS")
    os.environ["ZHUB_PEERS"] = f"http://127.0.0.1:{stub_port}"

    def run_hub():
        cfg = uvicorn.Config(create_app(), host="127.0.0.1", port=hub_port,
                             log_level="warning")
        asyncio.run(uvicorn.Server(cfg).serve())

    threading.Thread(target=run_stub, daemon=True).start()
    _wait(stub_port)
    threading.Thread(target=run_hub, daemon=True).start()
    _wait(hub_port)

    # Restore env (other tests should not be affected)
    if env_backup is None:
        os.environ.pop("ZHUB_PEERS", None)
    else:
        os.environ["ZHUB_PEERS"] = env_backup

    # Request without any Authorization header → api_key_header == ""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"http://127.0.0.1:{hub_port}/remote-bot/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hi"}],
            },
            # Deliberately no Authorization header
        )

    # Hub A may respond with the stub's body or an error — what we care
    # about is what was sent upstream.
    auth_value = received_headers.get("authorization", "MISSING")
    assert auth_value == "MISSING", (
        f"Hub A forwarded a blank/present Authorization header to the peer: "
        f"'authorization: {auth_value}'. "
        "When the client provides no api_key the header must be OMITTED."
    )
