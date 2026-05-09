"""Wrap an MCP server as a zhub publisher.

This bridge spawns any MCP server as a subprocess (via `command`), wraps it,
and publishes it through zhub. The MCP server's tools become zhub
capabilities; chat requests get routed to the appropriate tool.

Why this matters: MCP is gaining adoption (Claude Desktop, Cline, MCP
Inspector, etc.). This bridge lets ANY MCP server become reachable as a
zhub publisher — and through that, callable from anywhere via standard
OpenAI Chat Completions.

Two routing strategies for chat -> tool:

1. Single-tool mode (default): if the MCP server exposes exactly one
   tool, every chat request is routed to it with the user's last message
   as a string argument. Simple and works for most useful MCP servers.

2. Tool-name-prefix mode: the user's message can start with `<tool>:`
   to route to a specific tool — e.g. "read_file: /tmp/notes.md".

Run:

    pip install zhub httpx
    HUB_URL=ws://localhost:8080 \\
        MCP_COMMAND="uvx mcp-server-everything" \\
        python examples/mcp_bridge.py
"""

import asyncio
import logging
import os
import shlex

from zhub import publish, Capability
from zhub.mcp import MCPClient, MCPError


HUB_URL = os.environ.get("HUB_URL", "ws://localhost:8080")
MCP_COMMAND = os.environ.get("MCP_COMMAND", "")
MCP_NAME = os.environ.get("MCP_NAME", "mcp-bridged")
MCP_DESCRIPTION = os.environ.get("MCP_DESCRIPTION", "MCP server bridged via zhub")
MCP_PUBLIC = os.environ.get("MCP_PUBLIC", "0") == "1"


def _parse_routing(text: str) -> tuple[str | None, str]:
    """Return (tool_name, body) — tool_name=None means use single-tool default."""
    if ":" in text:
        head, _, rest = text.partition(":")
        head = head.strip()
        if head.replace("_", "").replace("-", "").isalnum():
            return head, rest.strip()
    return None, text


async def main():
    logging.basicConfig(level=logging.INFO)
    if not MCP_COMMAND:
        raise SystemExit(
            "MCP_COMMAND env var required. example:\n"
            "  MCP_COMMAND='uvx mcp-server-everything' python examples/mcp_bridge.py"
        )

    cmd = shlex.split(MCP_COMMAND)
    mcp = MCPClient(cmd)
    print(f"starting MCP subprocess: {' '.join(cmd)}")
    await mcp.start()
    tools = await mcp.list_tools()
    print(f"MCP server exposes {len(tools)} tool(s): {[t['name'] for t in tools]}")

    capabilities = [
        Capability(
            name=t["name"],
            description=t.get("description", ""),
            schema=t.get("inputSchema") or {"type": "object"},
        )
        for t in tools
    ]
    tools_by_name = {t["name"]: t for t in tools}

    async def chat_handler(messages, options):
        last = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last = m.get("content", "")
                break

        explicit_tool, body = _parse_routing(last)
        if explicit_tool and explicit_tool in tools_by_name:
            tool_name = explicit_tool
            args_text = body
        elif len(tools) == 1:
            tool_name = tools[0]["name"]
            args_text = last
        else:
            return (
                f"This MCP server exposes {len(tools)} tools "
                f"({', '.join(tools_by_name.keys())}). "
                f"Prefix your message with '<tool_name>: <input>' to route. "
                f"Example: 'echo: hello'"
            )

        schema = tools_by_name[tool_name].get("inputSchema") or {}
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if properties:
            if len(required) == 1:
                args = {required[0]: args_text}
            else:
                first_prop = next(iter(properties.keys()))
                args = {first_prop: args_text}
        else:
            args = {"text": args_text}

        try:
            result = await mcp.call_tool(tool_name, args)
        except MCPError as e:
            return f"[mcp error] {e}"

        content_parts = result.get("content", [])
        text_pieces = [p.get("text", "") for p in content_parts if p.get("type") == "text"]
        return "".join(text_pieces) or str(result)

    pub = publish(
        name=MCP_NAME,
        description=MCP_DESCRIPTION,
        chat_handler=chat_handler,
        hub_url=HUB_URL,
        capabilities=capabilities,
        public=MCP_PUBLIC,
    )

    while not pub.api_key:
        await asyncio.sleep(0.1)

    print()
    print("=" * 64)
    print(f"  MCP server published as zhub")
    print(f"  Name:    {pub.name}")
    print(f"  Hub:     {HUB_URL}")
    print(f"  API key: {pub.api_key}")
    print(f"  Tools:   {[t['name'] for t in tools]}")
    print("=" * 64)
    print()
    print("  Ctrl-C to stop.")

    try:
        while True:
            await asyncio.sleep(60)
    finally:
        await mcp.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        print("[mcp_bridge] shutting down.")
