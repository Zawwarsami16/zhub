"""Tiny MCP (Model Context Protocol) client over stdio.

MCP is Anthropic's protocol for AI tools/resources/prompts to expose
themselves to LLM clients. The wire format is JSON-RPC 2.0; for stdio
transports, each message is a single JSON object on its own line.

This is the *minimum* MCP client needed to wrap an MCP server as a zhub
publisher: connect, initialize, list tools, call tool. No support yet for
resources, prompts, or notifications. Streaming tool results are not
required for our wrapping use case (zhub call_tool is request-response).

Usage:

    from zhub.mcp import MCPClient

    async def main():
        c = MCPClient(["uvx", "mcp-server-filesystem", "/tmp"])
        await c.start()
        try:
            tools = await c.list_tools()
            print([t["name"] for t in tools])
            r = await c.call_tool("read_file", {"path": "/tmp/foo.txt"})
            print(r["content"])
        finally:
            await c.close()
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger("zhub.mcp")


class MCPError(Exception):
    """Raised when an MCP server returns a JSON-RPC error or the transport fails."""


class MCPClient:
    """A JSON-RPC-over-stdio client for an MCP server subprocess."""

    def __init__(
        self,
        command: list[str],
        env: dict[str, str] | None = None,
        request_timeout: float | None = 60.0,
    ) -> None:
        if not command:
            raise ValueError("command list must not be empty")
        self.command = command
        self.env = env
        # Bound every request, not just initialize. A subprocess that stays
        # alive but never answers (hung tool, dropped request, protocol
        # desync) would otherwise hang the caller forever — only EOF and crash
        # resolve pending futures. None disables the bound (wait indefinitely).
        self.request_timeout = request_timeout
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._initialized = False

    async def start(self, init_timeout: float = 10.0) -> None:
        """Spawn the subprocess and complete the MCP `initialize` handshake."""
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        # Drain the subprocess's stderr. If we leave stderr=PIPE without a
        # consumer, an MCP server that logs verbosely fills the ~64KB pipe
        # buffer, blocks on its next stderr write, and stops answering any
        # request — every subsequent call then times out. Logging lines at
        # debug keeps them recoverable when the operator turns logging up.
        self._stderr_task = asyncio.create_task(self._stderr_drain())
        try:
            await asyncio.wait_for(
                self._request("initialize", {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "zhub.mcp", "version": "0.1"},
                }),
                timeout=init_timeout,
            )
            self._initialized = True
        except Exception:
            await self.close()
            raise

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the list of tools the server exposes."""
        if not self._initialized:
            raise MCPError("MCPClient.start() not called or initialize failed")
        result = await self._request("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call a tool by name with the given arguments. Returns the tool result.
        Raises MCPError if the server returns a JSON-RPC error (e.g. unknown tool).
        """
        if not self._initialized:
            raise MCPError("MCPClient.start() not called or initialize failed")
        return await self._request("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })

    async def close(self) -> None:
        """Cleanly shut down the subprocess + reader."""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
            except ProcessLookupError:
                pass
        self._fail_pending(MCPError("MCP subprocess terminated"))

    # ---- internals ----

    def _fail_pending(self, exc: Exception) -> None:
        """Resolve every in-flight request with `exc` so callers stop awaiting."""
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.process or not self.process.stdin:
            raise MCPError("subprocess not running")

        async with self._lock:
            self._next_id += 1
            req_id = self._next_id

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[req_id] = future

        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        wire = json.dumps(msg).encode("utf-8") + b"\n"
        try:
            self.process.stdin.write(wire)
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            self._pending.pop(req_id, None)
            raise MCPError(f"failed to send: {e}") from e

        try:
            return await asyncio.wait_for(future, timeout=self.request_timeout)
        except asyncio.TimeoutError as e:
            raise MCPError(
                f"request '{method}' timed out after {self.request_timeout}s"
            ) from e
        finally:
            self._pending.pop(req_id, None)

    async def _reader_loop(self) -> None:
        """Read JSON-RPC responses from stdout, dispatch to pending futures."""
        assert self.process and self.process.stdout
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    # EOF: the server closed stdout (exited or crashed). Any
                    # in-flight request will never get a response, so fail them
                    # instead of leaving callers awaiting forever.
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("mcp: discarding non-JSON line: %r", line[:120])
                    continue
                if "id" not in msg:
                    log.debug("mcp notification: %r", msg)
                    continue
                rid = msg["id"]
                fut = self._pending.get(rid)
                if fut is None or fut.done():
                    continue
                if "error" in msg:
                    err = msg["error"]
                    fut.set_exception(MCPError(
                        f"{err.get('message', 'unknown error')} "
                        f"(code={err.get('code', '?')})"
                    ))
                else:
                    fut.set_result(msg.get("result", {}))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("mcp reader loop crashed: %s", e)
            self._fail_pending(MCPError(f"reader crashed: {e}"))
            return
        # Clean EOF (the `break` above): subprocess closed its output stream.
        self._fail_pending(MCPError("MCP subprocess closed its output stream"))

    async def _stderr_drain(self) -> None:
        """Discard-with-debug-log drain for the subprocess's stderr.

        Runs alongside `_reader_loop`; consumes chunks so the pipe buffer
        never fills. Uses `read(N)` rather than `readline()` — a spammy MCP
        server that writes megabytes without a newline (progress bars, JSON
        blobs on one line) would otherwise accumulate past StreamReader's
        limit, pause the transport, and refill the pipe anyway. On EOF the
        drain returns cleanly — stdout EOF terminates the client, stderr EOF
        alone is expected on shutdown and carries no signal we act on.
        """
        assert self.process and self.process.stderr
        try:
            while True:
                chunk = await self.process.stderr.read(4096)
                if not chunk:
                    return
                log.debug("mcp stderr: %s", chunk.decode("utf-8", "replace").rstrip("\n"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("mcp stderr drain stopped: %s", e)
