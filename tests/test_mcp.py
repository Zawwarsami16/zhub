"""MCP bridge tests — speak JSON-RPC over stdio to a stub MCP-like subprocess.

The stub is a self-contained Python script defined inline that responds to
`initialize`, `tools/list`, and `tools/call` JSON-RPC requests over stdin/stdout.
No real MCP server dependency.
"""

import os
import sys
import tempfile

import pytest

from zhub.mcp import MCPClient


# A minimal stub MCP server. Reads JSON-RPC lines from stdin, writes responses
# to stdout. Supports just enough of the MCP wire to test our client.
STUB_SCRIPT = '''
import json, sys

TOOLS = [
    {"name": "echo",    "description": "echo input back", "inputSchema": {"type": "object"}},
    {"name": "shout",   "description": "uppercase input", "inputSchema": {"type": "object"}},
]

def respond(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params", {})
    if method == "initialize":
        respond(rid, {"protocolVersion": "2024-11-05", "serverInfo": {"name": "stub"}})
    elif method == "tools/list":
        respond(rid, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        if name == "echo":
            respond(rid, {"content": [{"type": "text", "text": args.get("text", "")}]})
        elif name == "shout":
            respond(rid, {"content": [{"type": "text", "text": str(args.get("text", "")).upper()}]})
        else:
            respond(rid, error={"code": -32601, "message": f"unknown tool: {name}"})
    else:
        respond(rid, error={"code": -32601, "message": f"unknown method: {method}"})
'''


@pytest.fixture
def stub_script_path():
    fd, path = tempfile.mkstemp(suffix="_stub.py")
    with os.fdopen(fd, "w") as f:
        f.write(STUB_SCRIPT)
    yield path
    os.unlink(path)


@pytest.mark.asyncio
async def test_mcp_client_lists_tools(stub_script_path):
    client = MCPClient([sys.executable, stub_script_path])
    await client.start()
    try:
        tools = await client.list_tools()
        names = {t["name"] for t in tools}
        assert "echo" in names
        assert "shout" in names
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_client_calls_tool(stub_script_path):
    client = MCPClient([sys.executable, stub_script_path])
    await client.start()
    try:
        result = await client.call_tool("echo", {"text": "hello"})
        assert result["content"][0]["text"] == "hello"

        result = await client.call_tool("shout", {"text": "father"})
        assert result["content"][0]["text"] == "FATHER"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_client_unknown_tool_raises(stub_script_path):
    client = MCPClient([sys.executable, stub_script_path])
    await client.start()
    try:
        with pytest.raises(Exception) as exc_info:
            await client.call_tool("nonexistent", {})
        assert "unknown tool" in str(exc_info.value).lower() or "32601" in str(exc_info.value)
    finally:
        await client.close()
