# zhub entity — self-knowledge for any attached AI

> Single source of truth. If you are an AI (Claude, Llama, GPT, Ollama, or
> anything else) connected to a zhub hub, fetch this file once and you'll
> know how to use zhub fluently from message one. Routes, errors,
> performance tips, debug recipes — all here. Updated in lockstep with the
> hub's actual behavior.

**How to use:** `GET /entity` returns this whole file. `GET /entity/<section>`
returns just one section (`install`, `up`, `routes`, `errors`,
`patterns`, `debug`, `perf`, `paths`, `conventions`, `architecture`).
`GET /entity/errors/<code>` returns one error's recipe. On any 4xx/5xx
response the hub adds an `X-Zhub-Entity-Hint` header pointing at the
relevant section.

**You don't need a running hub to fetch this file.** The same content
ships in the package at `zhub/entity.md` — install zhub and read it
locally before bringing anything up.

---

## install

Smallest viable install on any Linux/Mac (Termux works too):

```bash
git clone https://github.com/Zawwarsami16/zhub
cd zhub
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[server,brains]'
```

Optional extras:
- `[crypto]` — adds `cryptography` for ed25519 signed manifests
- `[dev]`    — adds pytest + httpx for running the test suite

Verify the install with `python -m zhub doctor` — checks Python version,
imports, optional `cloudflared`, brain credentials in env, and prints
next-step commands.

## status

`python -m zhub status <hub-url>` pretty-prints a remote hub's state
(publishers, exposures, recent activity, latency percentiles, peers)
by hitting `<url>/api/dashboard`. Add `--json` for raw output suitable
for scripting. No auth needed — same data the browser dashboard polls.

```bash
python -m zhub status https://hub.example.com
python -m zhub status http://127.0.0.1:8080 --json
```

## up

The fastest path from clone to a usable URL+key:

```bash
python -m zhub up                          # auto-detect brain, public tunnel ON
python -m zhub up --no-tunnel              # local URL only
python -m zhub up --brain ollama --name me # explicit brain + AI name
GROQ_API_KEY=gsk_... python -m zhub up     # provide creds inline
```

`up` boots the hub, optionally starts a Cloudflare quick-tunnel, picks
the first available brain, publishes, and prints `URL:` + `KEY:` lines
ready to paste into any OpenAI-compatible client. Ctrl-C tears the
whole thing down cleanly.

If no brain credentials are present, `up` still brings the hub online
but skips the publisher; you can start one separately with
`python examples/multi_brain_publisher.py`.

## paths

What ends up where on disk:

- `zhub.db` — SQLite, in the cwd by default. Holds publishers (so a
  `zk_` key survives restarts) and operator-added entity extensions.
  Override with `--db <path>` or pass `--db ""` to disable persistence.
- `~/.cloudflared/` — cloudflared's config dir (only relevant for
  named/persistent tunnels; quick-tunnels use no config).
- Default port: 8080. `up` falls back to a free port if 8080 is taken.
- Logs go to stderr at `info` level by default. `--log-level warning`
  to quiet them.
- `entity.md` ships inside the package at `zhub/entity.md`. The hub
  serves it from disk (or in-memory cache) at `GET /entity`.

## architecture

zhub is a router. Publishers (`zhub.publish(...)`) are AIs that hold a
WebSocket to the hub and serve chats. Connections (`zhub.connect(...)`) are
clients that expose capabilities back. Hub multiplexes both directions on
the same WS per side. State (registry, in-flight requests) lives in hub
process; persistence is SQLite. Public HTTP exposes OpenAI-compatible chat
completions, direct capability invoke, registry, manifest, metrics, and
this entity. Federation: hubs peer each other and proxy chat/WS for AIs
hosted elsewhere.

---

## routes

### `GET /healthz`
Liveness probe. Returns `{status, publishers}`. No auth.

### `GET /metrics` (`?format=prometheus` for text exposition)
Hub-wide counters (per-AI: chat_requests, rate_limited, peer_proxied,
tool_calls_resolved, http_invoke, request_count, total_latency_ms,
max_latency_ms, avg_latency_ms, **p50/p95/p99_latency_ms**). The
percentiles come from a per-AI ring buffer of the last 200 latencies —
recent behavior, not lifetime history. Use to track who's hot, who's
failing, and where the tail is. No auth (snapshot only, no secrets).

Append `?format=prometheus` for OpenMetrics text exposition consumable
by Prometheus, VictoriaMetrics, Datadog OpenMetrics agents, etc.
Counter metrics use the `_total` suffix; per-AI metrics carry an `ai`
label.

### `GET /` and `GET /api/dashboard`
`/` serves an HTML operator dashboard (auto-refresh 3s). `/api/dashboard`
returns the JSON snapshot it polls — same data shape as `/metrics` plus
publisher/exposure details and a 50-entry recent-requests ring buffer.
No auth on either; public visibility into hub health.

### `GET /v1/models`
OpenAI-style model list — every registered publisher as one model. Use
when adding zhub as a custom OpenAI provider in BYOK clients (Pocket,
openai-py, etc.) — the client calls this on save to validate the provider.

### `GET /<ai>/v1/models`
Per-AI model list. Single entry. Use when base URL is `<hub>/<ai>/v1`.

### `GET /<ai>/manifest.json`
Publisher's full manifest: name, description, capabilities, signed
status, public_key, connected clients with their capabilities, plus
optional `resources` and `prompts` arrays declared at publish time
(Phase 9.0). The MCP server bridge (`zhub.mcp_server`) reads this on
each `*/list` to surface the AI's resources + prompts to MCP hosts.

### `GET /registry`
Public listing of currently-registered publishers (only ones marked
`public: true`). For discovery UIs.

### `GET /registry/global`
Local + federated peer listings, annotated with origin URL. Hubs
configured with `ZHUB_PEERS` aggregate here.

### `POST /<ai>/v1/chat/completions`  ← *the main one*
OpenAI Chat Completions, exact wire shape. Body: `{messages, model?,
temperature?, max_tokens?, stream?, tools?, tool_choice?}`. Auth: `Bearer
<api_key>` matching the publisher. Streams SSE if `stream:true`.

**Fast path tips:**
- If you already know which capability to call and the args, **skip chat
  entirely** — POST `/<ai>/v1/invoke` directly. Saves a full LLM
  round-trip.
- For tool-call flows: hub auto-resolves any `tool_calls` whose name
  matches a connected client capability, feeds results back as `role:tool`
  messages, returns final text. Set `X-Zhub-Tool-Resolve: client` to opt
  out and get tool_calls verbatim.
- Connected client capabilities are auto-injected as `tools` in the
  chat-request — you don't need to declare them. They appear in
  `options.tools` to the publisher.

### `POST /<ai>/v1/invoke`
Direct capability invocation. Body: `{capability: str, args: dict,
connection_id?: str}`. Auth: `Bearer <api_key>`. Auto-finds first
connection exposing the cap if `connection_id` omitted. Args are
validated against the cap's JSON schema before invoke. Returns
`{ok, result, connection_id}`. **Use this when you don't need LLM
reasoning** — fastest path, lowest cost.

### `GET /entity`, `GET /entity/<section>`, `GET /entity/errors/<code>`
This file. Served plain markdown. The full file and per-section views
also include any operator-added extensions for that hub.

### `POST /entity/extend`
Append an operator's own recipe to the entity. Auth: any registered
publisher's `Bearer <api_key>`. Body: `{section, title, body}`. Caps:
8 KB per body, 200 extensions per hub. Persists across restarts (only
when the hub started with `--db <path>`). The extension surfaces in
`GET /entity` (in an appendix), in `GET /entity/<section>` (appended
inside the section), and in `GET /entity/errors/<code>` (when the
title matches the code) — so any AI fetching the entity sees what
this hub specifically has learned.

### `GET /entity/extend`
List all extensions on this hub. Same auth as POST. Returns
`{extensions: [{id, section, title, body, added_by, added_at}]}`.

### `DELETE /entity/extend/{id}`
Remove an extension. Same auth.

### `WS /ws/publish`
Publisher long-lived WebSocket. Send `register-publisher` first.

### `WS /ws/connect`
Client long-lived WebSocket. Send `register-connection` first. If the
AI lives on a peer hub, this hub transparently tunnels.

### `GET /hub/identity` (Phase 17.0)
Returns this hub's long-lived identity: `{hub_id, version, signed,
public_key}`. `signed: false` means the `[crypto]` extras aren't
installed and this hub can't sign cross-hub requests or verify incoming
ones (still functional, just unverified). Other hubs fetch this once
and cache the public_key to verify signed peer routing.

When forwarding to a peer, the hub adds:
- `X-Zhub-Hub-Id: <our-id>`
- `X-Zhub-Hub-Signature: <ed25519-sig of forwarded-by-chain>`

Receiving hub fetches the originator's `/hub/identity`, caches, verifies.
Backwards compatible: missing signature = unverified (processed). Bad
signature with `ZHUB_REQUIRE_VERIFIED_PEERS=1` env set = `401
peer_unverified`. Bad without strict env = log warning, accept.

### `WS /ws/expose` (Phase 7.0)
Device-only WebSocket — no AI pairing required. Send `register-exposure`
first; hub returns `{exposure_id, device_key}` (`ex_...` and `dx_...`).
Device idles and listens for `invoke-request` envelopes. Re-registration
with the same `device_key` across hub restarts restores the same
`exposure_id` (persistence required: `--db <path>`). Any registered
publisher's `zk_` key can invoke this exposure via the HTTP route below.

### `GET /exposures`
Public-flagged exposures only. No auth. Returns
`[{exposure_id, name, description, capabilities, uptime_seconds}]`.

### `POST /exposures/<id>/invoke`
Auth: `Bearer <any registered publisher's zk_ key>`, subject to the
exposure's optional `allow_publishers` access policy. Body:
`{capability, args}`. Args validated against the exposure's declared
JSON schema. Returns `{ok, result, exposure_id}`.

The exposure's owner can pass `allow_publishers=["zai", "claude-here"]`
at registration time to whitelist which AIs may invoke. A 403 is
returned for callers not in the list. `allow_publishers=[]` (empty
list) is a kill switch — nobody can invoke. Unset = open (default).
The `/exposures` listing surfaces the policy when set so callers can
discover whether they're permitted before attempting.

---

## errors

Every error response includes an `X-Zhub-Entity-Hint` header pointing at
the relevant entry below.

### `400 missing 'capability'` (on `/v1/invoke`)
Body must include a non-empty `capability` field. Re-check JSON shape.

### `400 validation failed: ...`
Tool-call args (or `/v1/invoke` args) failed JSON-schema check against the
capability's declared schema. Error text names the failing field.
**Fix:** make args match the schema. The capability's schema is in the
publisher's `manifest.json` under `connections[].client_manifest.capabilities[].schema`.

### `401 invalid api key for this AI`
Bearer key doesn't match the registered publisher for `<ai>`. Two common
causes:
1. Wrong key — check the key was generated by THIS hub for THIS AI.
   Each hub generates its own keys; they don't roam.
2. AI was re-registered with a fresh key — old keys are invalidated. If
   you used persistence (`--db <path>`), the SAME key is preserved across
   hub restarts; without persistence you get a new key every time.

### `404 AI 'X' not found locally or in peers`
Publisher named `X` is not online here, and no configured peer has it
either. Check: (a) publisher process running? (b) registered with the
expected name? (c) if cross-hub: peer URL in `ZHUB_PEERS` env, peer
reachable, peer has the publisher.

### `404 AI offline`
Publisher disconnected mid-request. Check publisher logs — usually a WS
drop. With persistence the publisher will re-register on reconnect with
the same key.

### `404 capability 'X' not available on any connection`
No connected client exposes capability `X`. Check `manifest.json`
`connections[]` to see what's actually exposed.

### `429 rate limit exceeded`
Sliding-window quota for this api_key is full. `Retry-After` header tells
you how many seconds until the oldest hit expires. Either wait, or
re-register publisher with a higher `manifest.rate_limit` (e.g.
`"600/min"`).

### `502 invoke failed`
Capability handler raised. The handler's error message is in the response
body. Check the connected client's logs.

### `504 AI did not respond in time` / `504 capability did not respond in time`
Publisher / connection didn't reply within the request timeout (default
60s). Check that side is actually processing (not stuck, not OOMed).

### `508 loop_detected`
The `X-Zhub-Forwarded-By` chain already contains THIS hub's id, meaning
A→B→A peer routing was about to recurse. Check peer config — usually
two hubs that peer each other for the same AI name.

---

## patterns

### **Use a connected capability without thinking** (cheapest, fastest)
```
POST /<ai>/v1/invoke
{ "capability": "send_whatsapp", "args": {"to": "Ammi", "message": "ok"} }
```
Skip the LLM. Use when you (the calling AI/agent) already know the
intent.

### **Have the AI decide which tool to use** (LLM in the loop, hub auto-resolves)
```
POST /<ai>/v1/chat/completions
{ "messages": [{"role":"user","content":"send Ammi a message"}] }
```
Hub injects available capabilities as OpenAI `tools`. Publisher's brain
emits `tool_calls`. Hub auto-invokes connected caps, feeds back results,
returns final text. You get one round-trip externally even if the LLM
took multiple internal turns.

### **Streaming for long responses**
Add `"stream": true`. Hub returns OpenAI SSE chunks. Three modes for
how tool_calls are handled in streaming responses, controlled by the
`X-Zhub-Stream-Tools` header:

- **(unset, default)** — tool_call deltas pass through as standard
  OpenAI SSE chunks (`delta.tool_calls`). Client handles them. Hub
  doesn't auto-resolve. Matches what Cursor / Continue / native
  OpenAI SDK with `stream=True` expect.
- **`auto`** — pass deltas through AND when `finish_reason: tool_calls`
  arrives, hub auto-invokes connected capabilities + exposures in
  parallel, appends a `role: tool` message, re-asks the publisher,
  and continues the same SSE stream with the follow-up. Bounded at
  4 hops.
- **`pre-resolve`** — buffer the full publisher response, run the
  non-streaming auto-resolve loop, emit the resolved final text as
  one SSE chunk + done. Trades stream latency for absolute correctness
  before any byte goes back to the client.

### **Cross-hub federation**
Configure `ZHUB_PEERS=http://hub-b.example.com,http://hub-c.example.com`
on hub A. Now A's `/registry/global` aggregates B and C. A `/<ai>/v1/...`
call for an AI on B or C is transparently proxied. Loop prevention via
`X-Zhub-Forwarded-By`.

### **Re-register after restart (preserve the same `zk_` key)**
Pass `api_key=<existing_zk_key>` to `zhub.publish(...)`. With `--db <path>`
on the hub, the publisher record persists; re-registration with the same
hashed key gets the same name + key back.

---

## debug

### "Pocket added the provider but no model is selectable"
1. CORS: `curl -i -X OPTIONS <hub>/<ai>/v1/chat/completions -H "Origin: https://x.example"` — expect `access-control-allow-origin: *`. Without CORS the browser blocks before the hub sees anything.
2. `/v1/models`: `curl <hub>/<ai>/v1/models -H "Authorization: Bearer <key>"` — should return `{object: list, data: [...]}`.
3. Hub log: empty hub log = browser blocked at CORS layer.

### "Chat returns immediately with empty body"
Publisher's chat_handler returned None or raised silently. Check
publisher logs.

### "WS dies every minute"
Likely an idle-timeout middlebox in front of cloudflared. Add app-level
ping every 30s. The protocol envelope includes `ping`/`pong` for this.

### "Tool calls aren't auto-resolving"
Check: (a) publisher actually emits openai-shape `tool_calls` in
chat-response payload, (b) `tool_calls[].function.name` matches a
capability name on a connected client, (c) request didn't carry
`X-Zhub-Tool-Resolve: client` (which forces pass-through).

### "Same `zk_` key generates different api_key each restart"
You started the hub without `--db`. With no persistence, each restart
allocates fresh keys. Fix: `python -m zhub.server --db zhub.db`.

### "Pocket session goes to wrong AI"
Pocket sends the model name in the body. zhub doesn't actually use it
for routing — routing is by URL path (`/<ai>/v1/...`). If the URL is
right, you're hitting the right AI regardless of model field.

---

## perf

- **POST /v1/invoke for known calls** is ~10-100× faster than LLM round-trip and uses zero brain tokens.
- **Parallel tool calls**: emit multiple `tool_calls` in a single chat-response — hub fans them out via `asyncio.gather`. Latency = max(handlers), not sum.
- **Persistence is cheap**: SQLite, single-writer, fine for 10k+ publishers. Always pass `--db` in production.
- **CORS allow-all is safe** for chat endpoints because auth is via Bearer key, not origin.
- **Cloudflare Tunnel adds ~30-80ms** of edge latency — fine for chat, painful for streaming inner-loop. For lowest local latency use a direct port + nginx instead.
- **Brain choice dominates total latency.** Hub overhead is ~5-15ms; the brain is everything from there. Pick brain by use case (Groq for chat speed, Ollama for $0, Sonnet for reasoning quality).

---

## conventions

- API keys start with `zk_` followed by URL-safe base64. Detect provider by prefix.
- Publisher names are URL-path components: `[A-Za-z0-9_-]+`.
- Capability names follow the same shape and act as OpenAI function-tool names verbatim.
- Hub IDs (`X-Zhub-Forwarded-By`) are random per-process unless `ZHUB_HUB_ID` is set.
- Manifests are JSON over WebSocket; the same JSON shape is what `manifest.json` returns over HTTP.

---

## changelog

This entity ships with each hub release. To check what version you're
talking to, fetch `/healthz` (returns publishers count, no version yet)
or check the hub's `pyproject.toml` `version` field. Public roadmap and
phase ledger live in the repo's `README.md` and `CLAUDE.md`.
