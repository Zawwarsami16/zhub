"""Run a published zhub AI as an MCP server over stdio.

Inverse of `zhub.mcp` (which wraps an MCP server as a zhub publisher).
This module exposes a remote zhub publisher's chat endpoint as an MCP
tool, so Claude Desktop / Cursor / Cline / any MCP-aware client can
talk to a zhub AI without knowing anything about zhub.

Wire protocol: line-delimited JSON-RPC 2.0 over stdin/stdout, per the MCP
stdio transport spec. We implement the minimum needed for tools:

  initialize      → handshake + capabilities advertisement
  tools/list      → expose `chat` (and discovered AI capabilities someday)
  tools/call      → forward `chat` to <hub>/<ai>/v1/chat/completions

Run:

    python -m zhub.mcp_server --hub https://hub.example.com \\
                              --ai my-ai \\
                              --key zk_xxxxxxxxxxxxxx
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Optional

try:
    import httpx
except ImportError as e:
    raise SystemExit(
        "zhub.mcp_server requires httpx. install:\n"
        "    pip install httpx"
    ) from e


log = logging.getLogger("zhub.mcp_server")

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "zhub.mcp_server", "version": "0.1"}
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


def _ok(req_id: Any, result: dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result})


def _err(req_id: Any, code: int, message: str, data: Optional[Any] = None) -> str:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "error": err})


class ZhubMCPServer:
    def __init__(self, hub: str, ai: str, key: str, timeout: float = 60.0) -> None:
        self.hub = hub.rstrip("/")
        self.ai = ai
        self.key = key
        self.timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None
        self._initialized = False
        # cap_name → connection_id, refreshed on each tools/list
        self._cap_index: dict[str, str] = {}

    async def start(self) -> None:
        self._http = httpx.AsyncClient(timeout=self.timeout)

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()

    async def _discover_capabilities(self) -> list[dict[str, Any]]:
        """Fetch the AI's manifest and turn connected client capabilities
        into MCP tool entries. Returns [] if the AI is offline or no
        connections are present — `chat` is always exposed independently."""
        assert self._http is not None
        try:
            resp = await self._http.get(f"{self.hub}/{self.ai}/manifest.json")
            if resp.status_code != 200:
                return []
            manifest = resp.json()
        except Exception:
            return []
        tools: list[dict[str, Any]] = []
        self._cap_index.clear()
        for conn in manifest.get("connections") or []:
            conn_id = conn.get("connection_id", "")
            for cap in (conn.get("client_manifest") or {}).get("capabilities") or []:
                name = cap.get("name") or ""
                if not name or name in self._cap_index:
                    continue
                self._cap_index[name] = conn_id
                tools.append({
                    "name": name,
                    "description": cap.get("description") or f"zhub capability {name}",
                    "inputSchema": cap.get("schema") or {"type": "object"},
                })
        return tools

    async def tools(self) -> list[dict[str, Any]]:
        chat_tool = {
            "name": "chat",
            "description": (
                f"Send a prompt to the zhub-published AI '{self.ai}' "
                f"(via {self.hub}). Returns the AI's reply as text."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "User message to send to the AI.",
                    },
                },
                "required": ["prompt"],
            },
        }
        cap_tools = await self._discover_capabilities()
        # `chat` first, then capabilities, deduped (chat name is reserved)
        return [chat_tool] + [t for t in cap_tools if t["name"] != "chat"]

    async def call_capability(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a discovered capability via the hub's /v1/invoke endpoint."""
        assert self._http is not None
        resp = await self._http.post(
            f"{self.hub}/{self.ai}/v1/invoke",
            json={"capability": name, "args": arguments},
            headers={"Authorization": f"Bearer {self.key}"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"invoke failed ({resp.status_code}): {resp.text[:200]}")
        body = resp.json()
        text_repr = json.dumps(body.get("result"), indent=2, ensure_ascii=False)
        return {
            "content": [{"type": "text", "text": text_repr}],
            "isError": False,
        }

    async def call_chat(self, arguments: dict[str, Any]) -> dict[str, Any]:
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("`prompt` must be a non-empty string")
        assert self._http is not None
        resp = await self._http.post(
            f"{self.hub}/{self.ai}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": prompt}]},
            headers={"Authorization": f"Bearer {self.key}"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"hub returned {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        text = body.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        }

    async def handle(self, msg: dict[str, Any]) -> Optional[str]:
        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        if method == "initialize":
            self._initialized = True
            return _ok(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })

        if method == "notifications/initialized":
            return None  # client tells us it's done; no response

        if method == "tools/list":
            return _ok(req_id, {"tools": await self.tools()})

        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            try:
                if name == "chat":
                    result = await self.call_chat(arguments)
                elif name in self._cap_index:
                    result = await self.call_capability(name, arguments)
                else:
                    # cache could be stale; refresh once before giving up
                    await self._discover_capabilities()
                    if name in self._cap_index:
                        result = await self.call_capability(name, arguments)
                    else:
                        return _err(req_id, JSONRPC_METHOD_NOT_FOUND,
                                    f"unknown tool: {name}")
            except ValueError as e:
                return _err(req_id, JSONRPC_INVALID_PARAMS, str(e))
            except Exception as e:
                return _err(req_id, JSONRPC_INTERNAL_ERROR, str(e))
            return _ok(req_id, result)

        if "id" in msg:
            return _err(req_id, JSONRPC_METHOD_NOT_FOUND, f"unknown method: {method}")
        return None


async def _stdio_loop(server: ZhubMCPServer) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    proto = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: proto, sys.stdin)
    out_writer = sys.stdout

    while True:
        line = await reader.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            continue
        try:
            response = await server.handle(msg)
        except Exception as e:
            log.exception("handler crashed")
            response = _err(msg.get("id"), JSONRPC_INTERNAL_ERROR, str(e))
        if response is not None:
            out_writer.write(response + "\n")
            out_writer.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="zhub MCP stdio server")
    parser.add_argument("--hub", required=True, help="hub base URL, e.g. https://hub.example.com")
    parser.add_argument("--ai", required=True, help="published AI name on the hub")
    parser.add_argument("--key", required=True, help="bearer api key for the AI")
    parser.add_argument("--log", default="warning", help="log level (default warning)")
    args = parser.parse_args()

    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, args.log.upper(), logging.WARNING),
        format="%(levelname)s zhub.mcp_server: %(message)s",
    )

    server = ZhubMCPServer(hub=args.hub, ai=args.ai, key=args.key)

    async def runner() -> None:
        await server.start()
        try:
            await _stdio_loop(server)
        finally:
            await server.close()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
