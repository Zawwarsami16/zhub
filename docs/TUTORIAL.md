# zhub in 10 minutes — hands-on

Goal: by the end, you have an OpenAI-compatible AI endpoint reachable from your phone. From `git clone` to "Pocket talks to your AI", in one sitting.

## Minute 0–2: install

```bash
git clone https://github.com/Zawwarsami16/zhub
cd zhub
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[server,brains]'
```

Sanity check:

```bash
python -m zhub doctor
```

You should see green checks for `import zhub`, the server deps, and `cloudflared` (if installed). Brain creds will all show ✗ unless you've already set env vars — that's fine, we'll fix it next.

## Minute 2–3: pick a brain

You need credentials for **one** of these — any of the eight will work. Pick by what you have or want to use:

| Brain | Why pick it | Get a key |
|---|---|---|
| **Ollama** | Free, runs on your machine, no signup | `ollama serve` then `ollama pull llama3.2` |
| **Groq** | Free tier, **700 tok/s**, fastest perceived | https://console.groq.com |
| **OpenAI** | Familiar, gpt-4o-mini is cheap | https://platform.openai.com |
| **Anthropic** | Best reasoning (Claude) | https://console.anthropic.com |
| **Together / Mistral / Cohere / Cerebras** | More options | their respective consoles |

For this tutorial we'll use **Groq** because the free tier is generous and replies are instant:

```bash
export GROQ_API_KEY=gsk_your_key_here
```

## Minute 3–4: bring it up

```bash
python -m zhub up
```

You'll see something like:

```
================================================================
  brain:    Groq Llama 3.3 70B
  URL:      https://stuck-bonus-eight-spider.trycloudflare.com/me/v1
  KEY:      zk_BWOuFb8-Fiw8JpVjWO3hNwCaTfASE_To
  paste both into Pocket / openai-py / curl / Claude Desktop
================================================================
```

That's it. `python -m zhub up` started:
- A hub server on port 8080
- A Cloudflare quick-tunnel (the `*.trycloudflare.com` URL is reachable from anywhere)
- A publisher that proxies chat to Groq
- Persistent SQLite at `./zhub.db` so the `zk_` key survives restarts

## Minute 4–6: try it from the command line

In another terminal:

```bash
curl -X POST https://stuck-bonus-eight-spider.trycloudflare.com/me/v1/chat/completions \
  -H "Authorization: Bearer zk_BWO..." \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"explain zhub in one sentence"}]}'
```

If that works, your AI is reachable from anywhere on the internet.

## Minute 6–7: open the dashboard

Open `http://localhost:8080/` in a browser. You'll see a live operator console — particles flow through the SVG every time a request lands. Stat tiles show publishers / connections / requests / p95 latency. The recent-requests feed scrolls in real time.

## Minute 7–9: plug it into a chat app

### Pocket (browser-based BYOK)

Pocket auto-detects zhub providers. Open Pocket → settings → add provider → paste:
- Base URL: `https://stuck-bonus-eight-spider.trycloudflare.com/me/v1`
- API Key: `zk_BWO...`

Save. The model `me` appears in the picker. Send a message — it round-trips through your hub.

### Claude Desktop / Cursor / Cline

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (or your client's MCP config):

```json
{
  "mcpServers": {
    "me": {
      "command": "python",
      "args": [
        "-m", "zhub.mcp_server",
        "--hub", "https://stuck-bonus-eight-spider.trycloudflare.com",
        "--ai", "me",
        "--key", "zk_BWO..."
      ]
    }
  }
}
```

Restart Claude Desktop. Your AI shows up as a `chat` tool. Anything connected to your hub via `expose()` (next section) shows up as additional MCP tools.

### openai-py

```python
from openai import OpenAI
client = OpenAI(
    base_url="https://stuck-bonus-eight-spider.trycloudflare.com/me/v1",
    api_key="zk_BWO...",
)
print(client.chat.completions.create(
    model="me",
    messages=[{"role": "user", "content": "hi"}],
).choices[0].message.content)
```

## Minute 9–10: add a tool

Your AI doesn't have a `weather_lookup` tool, but you can give it one in 30 lines. In a third terminal:

```python
# weather_sensor.py
import asyncio
from zhub import expose

e = expose(
    name="weather-sensor",
    capabilities={
        "weather_lookup": (
            {"type": "object", "required": ["city"],
             "properties": {"city": {"type": "string"}}},
            lambda args: {"city": args["city"], "temp_c": 14, "condition": "cloudy"},
        ),
    },
    hub_url="ws://127.0.0.1:8080",
    public=True,
)

async def main():
    while not e.exposure_id:
        await asyncio.sleep(0.05)
    print(f"weather sensor exposed as {e.exposure_id}")
    await asyncio.Event().wait()

asyncio.run(main())
```

Run it: `python weather_sensor.py`. Now the dashboard shows an "exposure" tile. Any AI on the hub can invoke this capability via `POST /exposures/<id>/invoke` with a publisher's bearer key. After Phase 1.9 capability injection lands for exposures, the AI will see it as a tool automatically.

## What you have now

A reusable, production-ready AI endpoint that:
- Speaks OpenAI Chat Completions wire format → works in any BYOK client
- Streams via SSE (Cursor / Continue compatible)
- Is reachable from anywhere via Cloudflare Tunnel
- Survives restarts (SQLite persistence keeps the `zk_` key stable)
- Has a live operator dashboard
- Routes tool calls to connected device-only "exposures"
- Bridges to Claude Desktop via MCP (chat + every connected capability becomes a tool)

## Next steps

- [`docs/DEPLOY.md`](DEPLOY.md) — deploy this on a $5 VPS so it stays up forever
- [`examples/`](../examples/README.md) — more runnable demos (federation, council, multi-brain, full-stack)
- `curl <hub>/entity` — zhub's self-knowledge: routes, errors, patterns, debug recipes
- `python -m zhub status <hub-url>` — pretty-print any remote hub's state

## Common pitfalls

| Symptom | Fix |
|---|---|
| `python -m zhub up` says "no brains available" | Set one of the env vars in step 2 |
| Cloudflared tunnel didn't start | `apt install cloudflared` (or skip with `--no-tunnel`) |
| Pocket says "couldn't auto-detect" | Make sure your URL ends with `/v1` (zhub provider matches that pattern) |
| `zk_` key changed after restart | You're missing `--db ./zhub.db` flag on the hub. `zhub up` sets it by default |
| Browser dashboard is blank | Check `http://localhost:8080/api/dashboard` returns JSON. If not, check `python -m zhub doctor` |
