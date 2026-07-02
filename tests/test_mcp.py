"""MCP bridge tests — speak JSON-RPC over stdio to a stub MCP-like subprocess.

The stub is a self-contained Python script defined inline that responds to
`initialize`, `tools/list`, and `tools/call` JSON-RPC requests over stdin/stdout.
No real MCP server dependency.
"""

import asyncio
import os
import sys
import tempfile

import pytest

from zhub.mcp import MCPClient, MCPError


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


# A stub that completes the handshake, then exits (closing stdout) on the first
# non-initialize request — i.e. the server dies mid-call without responding.
DYING_STUB_SCRIPT = '''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    rid = req.get("id")
    if req.get("method") == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {}}) + "\\n")
        sys.stdout.flush()
    else:
        sys.exit(0)
'''


@pytest.fixture
def dying_stub_path():
    fd, path = tempfile.mkstemp(suffix="_dying_stub.py")
    with os.fdopen(fd, "w") as f:
        f.write(DYING_STUB_SCRIPT)
    yield path
    os.unlink(path)


@pytest.mark.asyncio
async def test_in_flight_request_fails_when_subprocess_exits(dying_stub_path):
    # Regression: if the MCP server closes stdout (EOF) while a request is in
    # flight, the reader loop used to just stop, leaving the request future
    # unresolved forever. It must fail fast instead.
    client = MCPClient([sys.executable, dying_stub_path])
    await client.start()
    try:
        with pytest.raises(MCPError):
            await asyncio.wait_for(client.call_tool("echo", {"text": "x"}), timeout=5.0)
    finally:
        await client.close()


# A stub that completes the handshake then stays alive but silently ignores
# every subsequent request — no response, no EOF. The exact case the EOF fix
# doesn't cover: a hung/unresponsive-but-living server.
SILENT_STUB_SCRIPT = '''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    rid = req.get("id")
    if req.get("method") == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {}}) + "\\n")
        sys.stdout.flush()
    # any other method: read it and never answer, keeping the process alive
'''


@pytest.fixture
def silent_stub_path():
    fd, path = tempfile.mkstemp(suffix="_silent_stub.py")
    with os.fdopen(fd, "w") as f:
        f.write(SILENT_STUB_SCRIPT)
    yield path
    os.unlink(path)


@pytest.mark.asyncio
async def test_request_times_out_when_server_silent(silent_stub_path):
    # Regression: a subprocess that stays alive but never responds to a
    # request (no EOF, no crash) used to hang the caller forever — only
    # initialize was time-bounded. The per-request timeout must fail it.
    # No external asyncio.wait_for here on purpose: the client must bound
    # the wait itself, otherwise this test would hang the whole suite.
    client = MCPClient([sys.executable, silent_stub_path], request_timeout=1.0)
    await client.start()
    try:
        with pytest.raises(MCPError, match="timed out"):
            await client.call_tool("echo", {"text": "x"})
    finally:
        await client.close()


# A stub that floods stderr with 2 MB before answering initialize, then again
# on every tools/list. Without a drain, the ~64KB pipe buffer + StreamReader
# limit fills, the subprocess blocks on stderr.flush() and never gets to read
# stdin — initialize never completes.
STDERR_FLOOD_STUB_SCRIPT = '''
import json, sys
for _ in range(32):
    sys.stderr.write("x" * (64 * 1024))
    sys.stderr.flush()
sys.stderr.write("\\n")
sys.stderr.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    rid = req.get("id")
    method = req.get("method")
    if method == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {}}) + "\\n")
        sys.stdout.flush()
    elif method == "tools/list":
        for _ in range(32):
            sys.stderr.write("y" * (64 * 1024))
            sys.stderr.flush()
        sys.stderr.write("\\n"); sys.stderr.flush()
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"tools": []}}) + "\\n")
        sys.stdout.flush()
'''


@pytest.fixture
def stderr_flood_stub_path():
    fd, path = tempfile.mkstemp(suffix="_stderr_flood_stub.py")
    with os.fdopen(fd, "w") as f:
        f.write(STDERR_FLOOD_STUB_SCRIPT)
    yield path
    os.unlink(path)


@pytest.mark.asyncio
async def test_stderr_flood_does_not_block_subprocess(stderr_flood_stub_path):
    # Regression: stderr=PIPE without a consumer used to fill the pipe buffer
    # (+ asyncio StreamReader limit), pause the transport, and force the child
    # to block on stderr.flush() — no more stdin reads, no more stdout writes,
    # every request timed out. The drain task must consume stderr as it comes
    # in so the subprocess stays responsive.
    client = MCPClient([sys.executable, stderr_flood_stub_path])
    # Bounded externally: without the drain this would hang past init_timeout,
    # so an outer wait_for guarantees the test fails fast rather than
    # slowing the suite.
    await asyncio.wait_for(client.start(init_timeout=8.0), timeout=10.0)
    try:
        tools = await asyncio.wait_for(client.list_tools(), timeout=8.0)
        assert tools == []
    finally:
        await client.close()
