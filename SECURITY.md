# Security Policy

## Reporting a vulnerability

If you find a security issue in zhub — credential exposure, auth bypass, validation skip, or anything else that lets an unauthorized caller act on a hub or a connected publisher — **please don't open a public issue**.

Instead, email **zawwarsami16@gmail.com** with:

- A description of the issue
- Steps to reproduce
- The version / commit you tested against
- Your suggested mitigation (if any)

You'll get a response within 48 hours. Confirmed vulnerabilities are patched on `main` and noted in `CHANGELOG.md` once a fix is merged.

## Scope

In scope:
- The Python hub (`zhub/server.py`) — auth, validation, rate limiting, federation, tool resolution, capability invocation
- The Python publisher / connection / exposure SDK (`zhub/client.py`)
- The MCP server bridge (`zhub/mcp_server.py`)
- Brain adapters (`zhub/brains/`) when bugs allow injection or credential leakage
- The JS / Kotlin clients

Out of scope:
- Brain providers' own infrastructure (Groq, OpenAI, Anthropic, etc.) — report to them
- Cloudflare Tunnel — report to Cloudflare
- Operator misconfiguration (e.g., running the hub without `--db` and complaining about lost keys)

## Hardening defaults already in place

- Bearer-key auth required on every chat / invoke / extend endpoint
- API keys stored hashed in SQLite (raw key returned to publisher exactly once at registration)
- ed25519 signature verification + key pinning on signed manifests
- Sliding-window rate limiting per api_key
- JSON-Schema validation of tool-call args before invoke
- Cross-hub federation loop prevention via `X-Zhub-Forwarded-By` / register-connection `via` chain
- CORS allow-all is intentional on chat endpoints (auth is via Bearer, not origin)
- Manifests carry no secrets — public discovery surface is safe to share

## Known operational caveats

- The hub's metrics counters and entity extensions are stored in plain SQLite; back them up like any small file. They contain no secrets but operators may treat the hub_id as semi-private.
- The dashboard at `/` and `/api/dashboard` are public by default — they reveal publisher names, exposure capability names, and recent request paths. Run on an internal interface or behind a separate auth proxy if that's not acceptable.
