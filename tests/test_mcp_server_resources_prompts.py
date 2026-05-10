"""Phase 9.0 — MCP server bridge serves the full triple: tools + resources + prompts.

Reuses the same subprocess-driven JSON-RPC test pattern as test_mcp_server.py.
"""

import asyncio
import json
import os
import socket
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


@pytest.fixture(scope="module")
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


async def _send_recv(proc, method: str, params: dict, req_id: int) -> dict:
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    await proc.stdin.drain()
    while True:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
        if not line:
            raise AssertionError("mcp_server closed stdout")
        try:
            data = json.loads(line.decode())
        except json.JSONDecodeError:
            continue
        if data.get("id") == req_id:
            return data


async def _spawn_mcp(hub_http: str, ai: str, key: str):
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "zhub.mcp_server",
        "--hub", hub_http, "--ai", ai, "--key", key,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )


@pytest.mark.asyncio
async def test_initialize_advertises_all_three_surfaces(hub):
    hub_http = f"http://127.0.0.1:{hub}"
    pub = publish(name="mcp-init-bot", description="x",
                  chat_handler=lambda m, o: "ok",
                  hub_url=f"ws://127.0.0.1:{hub}")
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    proc = await _spawn_mcp(hub_http, pub.name, pub.api_key)
    try:
        r = await _send_recv(proc, "initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        }, 1)
        caps = r["result"]["capabilities"]
        assert "tools" in caps
        assert "resources" in caps
        assert "prompts" in caps
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_resources_list_and_read(hub):
    hub_http = f"http://127.0.0.1:{hub}"
    resources = [
        {
            "uri": "zhub://res-bot/readme",
            "name": "readme",
            "description": "the project readme",
            "mimeType": "text/markdown",
            "content": "# Project\n\nHello.",
        },
        {
            "uri": "zhub://res-bot/config",
            "name": "config",
            "mimeType": "application/json",
            "content": "{\"k\":1}",
        },
    ]
    pub = publish(name="res-bot", description="r",
                  chat_handler=lambda m, o: "ok",
                  hub_url=f"ws://127.0.0.1:{hub}",
                  resources=resources)
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    proc = await _spawn_mcp(hub_http, pub.name, pub.api_key)
    try:
        await _send_recv(proc, "initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        }, 1)
        listed = await _send_recv(proc, "resources/list", {}, 2)
        items = listed["result"]["resources"]
        assert {x["uri"] for x in items} == {
            "zhub://res-bot/readme", "zhub://res-bot/config",
        }
        # Each must have name, no content (read separately)
        names = {x["name"] for x in items}
        assert names == {"readme", "config"}

        read = await _send_recv(proc, "resources/read",
                                {"uri": "zhub://res-bot/readme"}, 3)
        contents = read["result"]["contents"]
        assert contents[0]["uri"] == "zhub://res-bot/readme"
        assert contents[0]["text"].startswith("# Project")
        assert contents[0]["mimeType"] == "text/markdown"

        bad = await _send_recv(proc, "resources/read",
                               {"uri": "zhub://nope"}, 4)
        assert "error" in bad
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_prompts_list_and_get_with_substitution(hub):
    hub_http = f"http://127.0.0.1:{hub}"
    prompts = [
        {
            "name": "summarize",
            "description": "summarize text in 3 bullets",
            "arguments": [
                {"name": "text", "required": True,
                 "description": "the text to summarize"},
            ],
            "messages": [
                {"role": "user",
                 "content": "Summarize this in 3 bullets:\n\n{text}"},
            ],
        },
    ]
    pub = publish(name="prompt-bot", description="p",
                  chat_handler=lambda m, o: "ok",
                  hub_url=f"ws://127.0.0.1:{hub}",
                  prompts=prompts)
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    proc = await _spawn_mcp(hub_http, pub.name, pub.api_key)
    try:
        await _send_recv(proc, "initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        }, 1)
        listed = await _send_recv(proc, "prompts/list", {}, 2)
        items = listed["result"]["prompts"]
        assert any(p["name"] == "summarize" for p in items)
        sumarize = next(p for p in items if p["name"] == "summarize")
        assert sumarize["arguments"][0]["name"] == "text"
        assert sumarize["arguments"][0]["required"] is True

        got = await _send_recv(proc, "prompts/get",
                               {"name": "summarize",
                                "arguments": {"text": "the rain in spain"}},
                               3)
        msgs = got["result"]["messages"]
        assert msgs[0]["role"] == "user"
        # MCP message content shape: {type: "text", text: "..."} OR plain string
        content = msgs[0]["content"]
        text = content if isinstance(content, str) else content.get("text", "")
        assert "the rain in spain" in text

        # Missing required arg
        bad = await _send_recv(proc, "prompts/get",
                               {"name": "summarize", "arguments": {}}, 4)
        assert "error" in bad

        # Unknown prompt
        nope = await _send_recv(proc, "prompts/get",
                                {"name": "doesnt-exist", "arguments": {}}, 5)
        assert "error" in nope
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
