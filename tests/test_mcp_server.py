"""Phase 2.1 — MCP server bridge.

Run a published zhub AI as an MCP server over stdio. Claude Desktop /
Cursor / Cline can then call it as if it were any other MCP tool.

The server speaks JSON-RPC 2.0 line-delimited on stdio:
  initialize  → handshake + advertise capabilities
  tools/list  → expose `chat` (and the AI's name in description)
  tools/call  → invoke `chat` with a `prompt` arg → forwards to zhub
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
def mcp_hub_port():
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn not installed")
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


async def _send_recv(proc: asyncio.subprocess.Process,
                     method: str, params: dict, req_id: int) -> dict:
    """Send a JSON-RPC request and wait for its response."""
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


@pytest.mark.asyncio
async def test_mcp_server_initialize_list_tools_call(mcp_hub_port):
    """Round-trip: spawn zhub.mcp_server as a subprocess wrapping a
    published AI, perform the standard MCP handshake, list tools, call
    the chat tool — should return the AI's response."""
    hub_ws = f"ws://127.0.0.1:{mcp_hub_port}"
    hub_http = f"http://127.0.0.1:{mcp_hub_port}"

    pub = publish(
        name="mcp-target",
        description="bridged via mcp",
        chat_handler=lambda m, o: f"saw user: {m[-1]['content']}",
        hub_url=hub_ws,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "zhub.mcp_server",
        "--hub", hub_http,
        "--ai", pub.name,
        "--key", pub.api_key,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
        # 1. initialize
        init_resp = await _send_recv(proc, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        }, req_id=1)
        assert "result" in init_resp, f"bad initialize: {init_resp!r}"
        assert init_resp["result"]["protocolVersion"]
        assert init_resp["result"]["serverInfo"]["name"] == "zhub.mcp_server"

        # 2. tools/list — expect at least `chat`
        tools_resp = await _send_recv(proc, "tools/list", {}, req_id=2)
        tools = tools_resp["result"]["tools"]
        names = [t["name"] for t in tools]
        assert "chat" in names, f"expected `chat` tool, got {names}"
        chat_tool = next(t for t in tools if t["name"] == "chat")
        assert "prompt" in chat_tool["inputSchema"]["properties"]

        # 3. tools/call chat
        call_resp = await _send_recv(proc, "tools/call", {
            "name": "chat",
            "arguments": {"prompt": "hello"},
        }, req_id=3)
        result = call_resp["result"]
        # MCP convention: result.content = [{type: "text", text: "..."}]
        content = result["content"]
        assert content[0]["type"] == "text"
        assert "saw user: hello" in content[0]["text"]

    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_mcp_server_unknown_tool_returns_error(mcp_hub_port):
    hub_http = f"http://127.0.0.1:{mcp_hub_port}"
    hub_ws = f"ws://127.0.0.1:{mcp_hub_port}"

    pub = publish(
        name="mcp-err",
        description="error case",
        chat_handler=lambda m, o: "ok",
        hub_url=hub_ws,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "zhub.mcp_server",
        "--hub", hub_http, "--ai", pub.name, "--key", pub.api_key,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        await _send_recv(proc, "initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        }, req_id=1)
        bad = await _send_recv(proc, "tools/call", {
            "name": "nonexistent", "arguments": {},
        }, req_id=2)
        assert "error" in bad, f"expected error, got {bad!r}"
        assert bad["error"]["code"] == -32601 or "unknown" in bad["error"]["message"].lower()
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
