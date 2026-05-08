# Publishing ZAI through zhub

> Make ZAI reachable from anywhere via standard OpenAI Chat Completions format. From your own friend's curl. From another AI. From Loki on your phone running anywhere.

## Prerequisites

1. **ZAI gateway running.** ZAI's `zai-openai-shim` plugin must be active on `127.0.0.1:7780` (the default). Verify:
   ```bash
   curl http://127.0.0.1:7780/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"messages":[{"role":"user","content":"ping"}]}'
   ```

2. **A zhub hub running somewhere.** Two options:

   **(a) Local laptop with public URL via Cloudflare Tunnel:**
   ```bash
   pip install 'zhub[server]'
   zhub-server --public-tunnel --db zhub.db
   # prints:  zhub public URL:  https://<random>.trycloudflare.com
   ```

   **(b) Local-only (LAN, dev):**
   ```bash
   zhub-server --port 8080 --db zhub.db
   ```

3. **`httpx` installed** alongside `zhub` for the proxy script:
   ```bash
   pip install zhub httpx
   ```

## Publish

```bash
HUB_URL=https://<your-hub>.trycloudflare.com \
ZAI_NAME=zai \
ZAI_PUBLIC=1 \
python examples/zai_publish.py
```

Output:

```
=================================================================
  ZAI published
  Name:        zai
  Hub:         https://<...>.trycloudflare.com
  Base URL:    https://<...>.trycloudflare.com/zai
  Manifest:    https://<...>.trycloudflare.com/zai/manifest.json
  API Key:     zk_a8f2c9d3e1b4...
=================================================================
```

Save the API key — you'll need it for every connecting client.

## Use ZAI from anywhere

### From curl

```bash
curl https://<hub>.trycloudflare.com/zai/v1/chat/completions \
  -H "Authorization: Bearer zk_a8f2c9d3..." \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"kya chal raha hai?"}]}'
```

### From the OpenAI Python library

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<hub>.trycloudflare.com/zai",
    api_key="zk_a8f2c9d3...",
)

response = client.chat.completions.create(
    model="zai-sonnet",
    messages=[{"role": "user", "content": "kaisa hai?"}],
)
print(response.choices[0].message.content)
```

### From Loki APK

See `kotlin/LOKI_INTEGRATION.md`. Loki uses zhub-kotlin's `connect(...)` to both call ZAI AND expose phone capabilities back.

### From another AI

Any AI that speaks OpenAI Chat Completions (including ZAI itself, GPT-4o-mini, Claude, etc.) can call ZAI via these endpoints. Multi-AI council patterns become trivial.

## Survive restarts

The `--db zhub.db` flag persists publisher records. The `ZAI_API_KEY` environment variable lets the publisher re-register with the same key after hub or process restart:

```bash
ZAI_API_KEY=zk_a8f2c9d3... python examples/zai_publish.py
```

Same name. Same URL. Same key. No re-distribution.

## What's actually proxied

The `zai_publish.py` script:

1. Opens a WebSocket to the hub.
2. Registers ZAI's manifest with `chat` + `introspect` + `memory_query` capabilities.
3. When the hub forwards a chat request, the script POSTs it to ZAI's local openai-shim (`http://127.0.0.1:7780/v1/chat/completions`).
4. Returns ZAI's response back through the WebSocket → hub → external client.

ZAI's full intelligence (entity, soul, memory, plugins, beliefs, council, vector retrieval) is in the loop — the proxy is thin.

## Connection events

The script prints when clients connect/disconnect:

```
[cx_a8f2c9d3] connected. capabilities: send_whatsapp, send_sms, open_app, get_battery
[cx_b9e3f7a1] connected. capabilities: send_message
[cx_a8f2c9d3] disconnected.
```

When Loki connects, ZAI knows its capabilities and can call them — see Loki integration doc for the bidirectional flow.

## Troubleshooting

- **"failed to register with hub"** — hub isn't running or HUB_URL wrong. Verify with `curl <hub-base-http>/healthz`.
- **"zai shim error"** — ZAI's openai-shim isn't listening on 7780. `curl http://127.0.0.1:7780/healthz` should return ok.
- **Connection drops after a few minutes** — Cloudflare ephemeral tunnels rotate URLs every restart. Use a named tunnel for stability, or run hub on a fixed VPS / NUC.
- **ZAI replies "I do not know"** — ZAI's full brain is in the loop; this is the brain answering, not zhub failing. Check ZAI's own state.

## Variables

| Env | Default | What |
|---|---|---|
| `HUB_URL` | `ws://localhost:8080` | hub WebSocket / HTTPS URL |
| `ZAI_SHIM_URL` | `http://127.0.0.1:7780/v1/chat/completions` | ZAI's local openai-shim endpoint |
| `ZAI_NAME` | `zai` | name registered on the hub |
| `ZAI_DESCRIPTION` | (set) | manifest description |
| `ZAI_OPERATOR` | `zawwar` | manifest operator field |
| `ZAI_PUBLIC` | `0` | set to `1` to appear in `/registry` listing |
| `ZAI_API_KEY` | unset | reuse this key on re-registration |
