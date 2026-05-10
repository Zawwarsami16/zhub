# zhub examples

Runnable scripts that show off each primitive in isolation, plus a few that combine them. Each is self-contained — no setup beyond a running hub (or, in some cases, the script spins one up itself).

## Index

| File | What it shows | Needs |
|---|---|---|
| [`publish_demo.py`](publish_demo.py) | Minimal `publish()` of a stateless echo AI | hub running |
| [`connect_demo.py`](connect_demo.py) | `connect()` a generic client + expose two capabilities back to a paired AI | hub + a published AI's name + key |
| [`orchestrate_demo.py`](orchestrate_demo.py) | All-in-one: AI publishes itself, client connects with capabilities, AI invokes them on demand | hub running |
| [`council_demo.py`](council_demo.py) | A "council" pattern — multiple AIs deliberate on a question | hub running |
| [`tool_demo.py`](tool_demo.py) | OpenAI tool-calling end-to-end (Phase 1.8): publisher emits `tool_calls`, hub auto-resolves against connected capabilities | hub running |
| [`federation_demo.py`](federation_demo.py) | Two hubs in one process; AI on hub B reachable through hub A via HTTP and WS | none (in-process) |
| [`mcp_bridge.py`](mcp_bridge.py) | Wrap an existing MCP server as a zhub publisher (subprocess + JSON-RPC over stdio) | hub + an MCP server binary (`MCP_COMMAND` env) |
| [`multi_brain_publisher.py`](multi_brain_publisher.py) | Pick a brain (auto-detect or explicit `--brain ollama|groq|openai|cerebras|anthropic|together|mistral|cohere`), publish, stream responses through hub | hub + brain credentials in env |
| [`full_stack_demo.py`](full_stack_demo.py) | The whole thing in one file: hub + publisher + exposure + tool resolution + dashboard pointer | none (in-process) |
| [`session_bridge_publisher.py`](session_bridge_publisher.py) | Wrap an interactive AI session (Claude Code, Cursor, custom agent loop) as a publisher via a file-bridge — chats become inbox files, the session writes outbox replies | hub + a watcher to read/reply (manual, an agent, or the built-in `_example_auto_watcher`) |

## Running

Most examples assume a hub is reachable at `ws://localhost:8080`. Start one in a separate terminal:

```bash
python -m zhub.server --port 8080
# or
python -m zhub up --no-tunnel --brain ollama
```

Then run the example:

```bash
python examples/publish_demo.py
```

## Wiring an example into Pocket / Claude Desktop / curl

Once an example publishes, the printed `URL:` and `KEY:` are usable from anywhere that speaks OpenAI Chat Completions:

```bash
# curl
curl -X POST http://localhost:8080/echo/v1/chat/completions \
  -H "Authorization: Bearer zk_..." \
  -d '{"messages":[{"role":"user","content":"hi"}]}'

# openai-py
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8080/echo/v1", api_key="zk_...")
client.chat.completions.create(model="echo", messages=[{"role":"user","content":"hi"}])
```

For Claude Desktop / Cursor / Cline, add to MCP config:

```json
{"mcpServers": {"echo": {
  "command": "python", "args": ["-m", "zhub.mcp_server",
    "--hub", "http://localhost:8080", "--ai", "echo", "--key", "zk_..."]
}}}
```

## Writing your own

Take `publish_demo.py` as a starting template — replace the `chat_handler` with whatever your brain is (a real LLM API, a local model, a stub). The rest of the surface (auth, tunnel, persistence, MCP, dashboard, federation) is the hub's job, not yours.

For wrapping existing services as publishers, `mcp_bridge.py` shows the subprocess + adapter pattern; `multi_brain_publisher.py` shows the env-driven brain selection pattern.
