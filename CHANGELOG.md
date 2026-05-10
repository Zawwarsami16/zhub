# Changelog

All notable changes to zhub. Versions follow [SemVer](https://semver.org/).

## [0.3.0] — 2026-05-10

### Added
- **MCP resources + prompts** (Phase 9.0) — publishers declare `resources=[...]` and `prompts=[...]` in `publish()`; the MCP bridge now serves the full triple (tools + resources + prompts) so Claude Desktop / Cursor / Cline see all three surfaces. Static-only for v1; dynamic round-trip is a future phase.
- **Per-AI latency percentiles** (Phase 10.0) — `/metrics` and `/api/dashboard` now include `p50_latency_ms`, `p95_latency_ms`, `p99_latency_ms` per AI, computed from a 200-sample ring buffer.
- **3 more brain adapters** (Phase 11.0) — Together, Mistral, Cohere. Brings total to 8: Ollama / Groq / OpenAI / Cerebras / Anthropic / Together / Mistral / Cohere. Refactored shared OpenAI-compat helper means future adapters are ~50 LOC.
- **Futuristic operator dashboard** (Phase 12.0) — animated SVG live traffic flow with particles per request, glassmorphism panels, neon HD blue/red palette, sparklines, recent-request feed, color-coded status, scanline overlay.
- **Public release polish** (Phase 13.0) — `CONTRIBUTING.md`, `CHANGELOG.md`, `SECURITY.md`. Multi-stage Dockerfile with non-root user + healthcheck. `zhub` CLI script alias. Python 3.13 in classifiers.
- **Distribution & ops** (Phase 14.0) — `docker-compose.yml` with optional cloudflared sidecar profile. `.github/workflows/release.yml` — tag-triggered PyPI publish + multi-arch GHCR Docker push. `py.typed` PEP 561 marker.
- **Per-exposure access policies** (Phase 15.0) — `expose(allow_publishers=["zai", "claude-here"])` whitelists which AIs may invoke. Empty list `[]` is a kill switch. Policy surfaces in `/exposures` listing.
- **`zhub status <hub-url>` CLI** (Phase 16.0) — pretty-print remote hub state from `/api/dashboard`. `--json` for scripting. GitHub issue/PR templates added with substrate-alignment checks. New `examples/full_stack_demo.py` runs the whole stack in one process.
- **Hub identity + signed peer routing** (Phase 17.0) — each hub generates a long-lived ed25519 keypair persisted in SQLite; `GET /hub/identity` exposes the public key. Cross-hub HTTP requests sign their forwarded chain; receiving hubs verify against the originator's published identity. Backwards-compatible (unsigned still accepted unless `ZHUB_REQUIRE_VERIFIED_PEERS=1`).
- **Prometheus metrics format** (Phase 18.0) — `/metrics?format=prometheus` returns OpenMetrics text exposition with per-AI counters labeled by `ai`. Drop-in for Prometheus / VictoriaMetrics / Datadog OpenMetrics agents.
- **`docs/TUTORIAL.md`** (Phase 18.0) — 10-minute hands-on walkthrough from clone to "Pocket talks to your AI".
- **`examples/README.md`** (Phase 18.0) — index of all 9 runnable examples.
- **Built-in browser chat client at `/chat`** (Phase 19.0) — self-contained 16 KB HTML chat UI in the same dark/cyan/neon palette. Auto-detects single-publisher hubs. SSE streaming with tool-call delta visualization. Settings persist to localStorage. Anyone can chat with their zhub AI without installing Pocket / curl / openai-py.

### Test surface
- 178 pytest, 13 node:test
- 19 numbered phases shipped post-0.2.0, every CI run green
- Spec + plan docs for each phase live under `docs/superpowers/specs/` and `docs/superpowers/plans/`

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
