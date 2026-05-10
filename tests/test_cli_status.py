"""`python -m zhub status <url>` round-trip test."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

try:
    import fastapi  # noqa
    import uvicorn  # noqa
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


@pytest.fixture
def hub():
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn not installed")
    port = _free_port()
    app = create_app()

    def run():
        config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                log_level="warning")
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
async def test_status_renders_remote_hub_state(hub):
    pub = publish(name="status-bot", description="status check",
                  chat_handler=lambda m, o: "ok",
                  hub_url=f"ws://127.0.0.1:{hub}",
                  public=True)
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "zhub", "status", f"http://127.0.0.1:{hub}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    out = stdout.decode()
    assert proc.returncode == 0, f"stderr={stderr.decode()!r}"
    assert "hub_id:" in out
    assert "PUBLISHERS" in out
    assert "status-bot" in out


@pytest.mark.asyncio
async def test_status_json_mode(hub):
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "zhub", "status", "--json",
        f"http://127.0.0.1:{hub}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    data = json.loads(stdout.decode())
    assert "hub_id" in data
    assert "publishers" in data


@pytest.mark.asyncio
async def test_status_unreachable_url_exits_nonzero():
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    # port 1 is reserved/blocked — guaranteed connection refused
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "zhub", "status",
        "--timeout", "1",
        "http://127.0.0.1:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    assert proc.returncode != 0
    assert "could not reach" in stderr.decode()
