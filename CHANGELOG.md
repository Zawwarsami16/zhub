# Changelog

All notable changes to zhub. Versions follow [SemVer](https://semver.org/).

## [0.2.0] — 2026-05-10

### Added
- **8 brain adapters** — Ollama, Groq, OpenAI, Cerebras, Anthropic, Together, Mistral, Cohere. Streaming-first; auto-detect via env vars; swap with one CLI flag.
- **`python -m zhub up`** — one-command bring-up (hub + optional Cloudflare tunnel + brain publisher), prints `URL:` and `KEY:` ready to paste into any OpenAI-compat client.
- **`python -m zhub doctor`** — environment + dependency + creds diagnostic.
- **`python -m zhub.mcp_server`** — exposes any zhub-published AI as an MCP stdio server (Claude Desktop / Cursor / Cline). Implements full MCP triple: tools, **resources, prompts**.
- **Capability-only WebSocket exposures** — devices register tools without pairing to any specific AI; any registered publisher can invoke via `POST /exposures/<id>/invoke`.
- **Tool calls** — auto-resolved against connected capabilities + exposures, parallel resolution via `asyncio.gather`, JSON-Schema arg validation, audit log in `usage.tool_results`.
- **Streaming tool calls** — `X-Zhub-Stream-Tools: auto` resolves and continues the SSE stream; `pre-resolve` buffers; default passes deltas through verbatim for native OpenAI clients.
- **Federation across hubs** — cross-hub HTTP chat proxy + cross-hub WebSocket connect tunnel, transparent to clients.
- **Self-knowledge entity layer** — `GET /entity` ships zhub's own routes/errors/patterns/install/up/paths recipes; operators extend per-deployment via `POST /entity/extend`. Every 4xx/5xx response carries `X-Zhub-Entity-Hint` pointing at the relevant recipe.
- **Operator dashboard at `/`** — futuristic glassmorphism UI with live SVG traffic flow visualization (particles per request, color-coded by status), publisher cards with sparklines, exposure tiles, recent-requests feed, federation peers. Auto-refresh 2s.
- **Production observability** — `/metrics` JSON snapshot with per-AI request count, total/avg/max/p50/p95/p99 latency. Structured access logs at `zhub.access` logger.
- **Cloudflare named tunnel support** — `python -m zhub up --tunnel-name <name>` for stable URLs across restarts.
- **`docs/DEPLOY.md`** — $5 VPS deployment walkthrough with systemd unit files for hub + named tunnel.
- **Multi-language clients** — JS/TS (`@zawwarsami/zhub`), Kotlin/JVM, Python all interoperate against the same hub.

### Changed
- Hub neutrality: removed all product-specific coupling. The substrate doesn't know about specific AIs or devices; bridges to specific products live in those products' own repos.
- README rewritten as a public-facing professional doc with hard performance numbers, mermaid architecture diagram, comparison matrix, brain pricing table, multi-language client matrix.

### Test surface
- 164 pytest (Python 3.10/3.11/3.12 in CI)
- 13 node:test (JS client)

## [0.1.0] — 2026-05-08

Initial release.

### Added
- `publish()` and `connect()` primitives over WebSocket
- Hub server (FastAPI + WebSocket multiplex)
- OpenAI-compatible chat completions endpoint per published AI
- SQLite persistence for publisher records
- ed25519 signed manifests with key pinning
- Cloudflare Tunnel auto-config (`--public-tunnel`)
- Bidirectional invocation: AI calls back into connected clients via `pub.invoke()`
- Sliding-window rate limiting per api_key
- Read-only federation via `ZHUB_PEERS` env
