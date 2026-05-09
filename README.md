# zhub

In Production

> WiFi for AIs. A drop-in skill that lets any AI publish a discoverable, controllable endpoint — and stay aware of every client connected to it. Bidirectional from day one.

---

## What this is

Today, when you build a custom AI (ZAI, an autonomous agent, a fine-tuned local model, anything), exposing it to the rest of the world means writing your own auth, your own tunnel, your own API gateway, your own client SDK. And once exposed, your AI has no idea who's connected to it or what those clients can do.

`zhub` is a tiny library + a tiny hub server that fixes both halves at once:

- **Publish mode** — the AI installs `zhub.publish(...)`, gets a public URL + API key, and is callable in standard OpenAI Chat Completions format. Anyone with the key can talk to it.
- **Connect mode** — clients (your phone bridge, your Telegram bot, your web chat, another AI) install `zhub.connect(...)`, expose their own capabilities back to the AI, and let the AI orchestrate them.

The result: **the AI is a hub, every client is a spoke, and every spoke is bidirectional.** Like WiFi pairing, but for AI agents.

---

## The 60-second demo

```bash
# 1. start the hub somewhere (your laptop, a free Render/Fly tier, your NUC)
python -m zhub.server --port 8080

# 2. in another terminal, publish your AI
python examples/publish_demo.py
# prints:  URL: http://localhost:8080/echo
#          KEY: zk_a8f2c9d3e1b4...

# 3. anyone with the key can call it like OpenAI:
curl http://localhost:8080/echo/v1/chat/completions \
  -H "Authorization: Bearer zk_a8f2c9d3e1b4..." \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hello"}]}'
```

That covers the publish side. Now the bidirectional half:

```bash
# 4. connect a client that exposes capabilities (e.g., fake Loki bridge)
AI_NAME=echo API_KEY=zk_... python examples/connect_demo.py
# the client now exposes send_whatsapp, get_battery to the AI
# the AI can invoke those capabilities through the hub
```

Or run the all-in-one orchestration demo:

```bash
python examples/orchestrate_demo.py
# >>> what connections do you have?
# <<< I have 1 connection(s): echo-client
# >>> tell me about my battery
# <<< invoked get_battery: {'level': 78, 'charging': False}
# >>> send a whatsapp to Ammi
# <<< invoked send_whatsapp: {'ok': True, 'to': 'Ammi', 'delivered': True}
```

The AI saw a connection arrive, registered its capabilities, and invoked them at the user's request — all through the hub, in a single conversation.

---

## Install

```bash
pip install zhub                  # client library only
pip install 'zhub[server]'        # also install the hub server
```

Or for development:

```bash
git clone https://github.com/Zawwarsami16/zhub.git
cd zhub
pip install -e '.[server,dev]'
pytest
```

---

## Library usage

### Publishing an AI

```python
from zhub import publish

def my_chat_handler(messages, options):
    # messages: OpenAI-format conversation
    # options: {model, temperature, max_tokens, ...}
    last_user = next((m["content"] for m in reversed(messages)
                      if m.get("role") == "user"), "")
    return f"You said: {last_user}"

pub = publish(
    name="my-ai",
    description="A simple example AI",
    chat_handler=my_chat_handler,
    hub_url="ws://localhost:8080",
    public=True,             # appears in /registry
    operator="me",
)

# Once registered, pub.api_key is the bearer token,
# pub.base_url is the URL prefix on the hub.
print(pub.name, pub.api_key, pub.base_url)
```

`pub.list_connections()` returns the currently-connected clients. `pub.find_capability("name")` returns the connection_id of any client offering a given capability. `pub.invoke(connection_id, capability, args)` calls back to that capability through the hub.

### Connecting a client + exposing capabilities

```python
from zhub import connect

def send_whatsapp(args):
    # args validated against the schema below
    return {"delivered": True, "to": args.get("to")}

conn = connect(
    ai_name="my-ai",
    api_key="zk_...",
    hub_url="ws://localhost:8080",
    capabilities={
        "send_whatsapp": (
            {
                "type": "object",
                "required": ["to", "message"],
                "properties": {
                    "to": {"type": "string"},
                    "message": {"type": "string"},
                },
            },
            send_whatsapp,
        ),
    },
)

# To send a chat to the AI from the client:
resp = await conn.chat(messages=[{"role": "user", "content": "hi"}])
print(resp["text"])

# The AI can now invoke conn's send_whatsapp through the hub.
```

---

## Architecture

```
[curl / openai-py / friend's app]
           │ HTTPS, Bearer key
           ▼
   ┌──────────────────────────┐
   │   zhub hub server         │
   │   • routes chat requests  │
   │   • routes invokes        │
   │   • holds the registry    │
   └────────┬─────────────────┘
            │  WebSocket multiplex
            │
   ┌────────┴──────────┐
   │                   │
[ AI publish() ]    [ Client connect() ]
   │ chat_handler      │ capability handlers
   │                   │
   └─── bidirectional ─┘
```

- The hub holds a registry of every published AI and every connection to it.
- Publishers receive `connection-event` messages whenever a client connects, disconnects, or updates its capabilities.
- Clients receive `invoke-request` messages when the AI wants to call a capability.

The wire protocol is plain JSON over WebSocket. See [`zhub/protocol.py`](zhub/protocol.py) for the full envelope schema.

---

## Manifest format (v0.1)

When an AI registers, it publishes a manifest like:

```json
{
  "schema_version": "0.1",
  "name": "my-ai",
  "description": "A simple example AI",
  "accepts": "openai-v1-chat-completions",
  "auth": {"type": "bearer"},
  "rate_limit": "60/min",
  "capabilities": [
    {"name": "chat", "description": "OpenAI chat completions", "schema": {...}}
  ],
  "public": true,
  "operator": "me",
  "endpoints": {
    "chat":     "/my-ai/v1/chat/completions",
    "manifest": "/my-ai/manifest.json",
    "registry": "/registry"
  },
  "connections": [
    {
      "connection_id": "cx_abc",
      "client_manifest": {...},
      "connected_for_seconds": 142
    }
  ]
}
```

A connecting client publishes a similar manifest describing the capabilities it exposes back to the AI.

---

## Why this exists

Every AI tool I've written ends up needing the same plumbing: a way to be reached, a way to be authenticated, a way to know what's connected to it. Existing options each handle a slice:

| Tool | Covers | Misses |
|---|---|---|
| ngrok / cloudflare tunnel | endpoint exposure | no manifest, no auth, no client awareness |
| Anthropic MCP | capability publishing, tool exposure | server-side, complex setup, AI-as-client framing |
| Google A2A | agent-to-agent | enterprise-focused, early |
| LangServe | chain endpoints | no auto-discovery, no bidirectional |
| OpenRouter | LLM aggregation | brings OpenRouter's models, not yours |
| Custom GPTs | chat-as-config | locked to OpenAI's ecosystem |

`zhub` is opinionated about one thing: the AI should be the hub, and discovering what's connected to it should be a one-liner from inside the AI's own code. Everything else (tunnels, auth, schema validation) is plumbing.

---

## Roadmap

- **0.1 (this release)** — publish, connect, hub server, JSON manifest, OpenAI-compat proxy, capability invocation.
- **0.2** — Cloudflare Tunnel auto-config so `publish()` works from any laptop without a public IP. MCP-format bridging.
- **0.3** — TypeScript/JavaScript client library so browser apps and Node services can `connect()`. Kotlin client for Android (Loki).
- **0.4** — Streaming responses end-to-end (SSE through the hub). Persistent connection registry across hub restarts. Public registry UI.
- **0.5** — Rate-limit policies, multi-tier API keys, audit log surface.
- **1.0** — Stable manifest schema, signed manifests, federation between hubs.

---

## License

MIT. See [`LICENSE`](LICENSE).

## Author

Zawwar Sami — github.com/Zawwarsami16
