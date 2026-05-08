# CLAUDE.md — brief for the next Claude who walks in

> Read top to bottom before touching anything. ~10 minute orientation. Sibling pattern to `~/.zai/CLAUDE.md` and `/root/ZTerminal/CLAUDE.md`.

---

## 1. Who everyone is

- **Father / Zawwar Sami** — the Commander. Builds tools as the joy itself, NOT for productization. Hinglish casual, direct. Has ZAI (autonomous AI son), Loki (phone body APK), Anteroom (publishing pipeline), ZTerminal/Joker (security tool — paused), and now zhub (this).
- **zhub** — drop-in library that turns any AI into a publishable, discoverable, controllable endpoint. **Bidirectional**: the AI is the hub, connected clients are spokes, AI sees everyone connected to it and can invoke their capabilities.
- **The hub server** — small FastAPI app that routes traffic. Runs on Father's machine (laptop, NUC, or a free PaaS tier).
- **Publishers** — AIs that call `zhub.publish(...)`. Examples: ZAI, future Joker brain, custom local models.
- **Clients/Connections** — anything that calls `zhub.connect(...)`. Examples: Loki (phone bridge), Telegram bot, web chat, another AI.
- **You (Claude)** — the tool Father uses to BUILD zhub. Not zhub. Stay in the right frame.

## 2. Repo + paths

| What | Path | Repo |
|---|---|---|
| zhub working tree | `/root/zhub/` | `Zawwarsami16/zhub` (public) |
| ZAI runtime (where publish() will be installed) | `/root/.zai/` | `Zawwarsami16/zai-runtime-private` |
| Loki APK (where connect() will be installed — Kotlin port phase 0.3) | `/root/.zai/loki-apk/` | `Zawwarsami16/Loki` |
| ZTerminal (sibling tool, paused) | `/root/ZTerminal/` | `Zawwarsami16/ZTerminal` |

CI: GitHub Actions on every push to `main`. Tests run on Python 3.10/3.11/3.12. No APK build — this is a library, not an app.

## 3. First thing to do on a fresh session

```bash
cd /root/zhub
git pull --ff-only

# Quick orient
cat README.md
cat CLAUDE.md
ls zhub/
ls examples/
git log --oneline -25

# Run the orchestration demo locally to verify everything works:
pip install -e '.[server,dev]'
python -m zhub.server --port 8080 &       # in one shell
python examples/orchestrate_demo.py       # in another
```

Tests: `pytest -v`. The e2e tests spin up the hub in-process and run the full publish+connect flow.

## 4. Historical arc

| Date | Event |
|---|---|
| 2026-05-08 morning | Father asked about Decepticon, then ZTerminal/Joker built. Disappointed with operational hollowness. |
| 2026-05-08 afternoon | Pivoted through zlang vision (general-purpose AI-native language). Then to AI-to-AI communication protocol. Then to abjad-style compression rule. |
| 2026-05-08 evening | Father asked about Anteroom Crypto Terminal app conversion. Drifted into Play Store framing — Father pulled the plug: *"meri taste i thinking tool building he hai"*. Memory updated. |
| 2026-05-08 evening | Father asked: *"can we build something jis ke through koi bhi custom ai like zai with just chat apna proper endpoint generate karde, more like API key"*. Initial response was ngrok-style tunnel exposure. |
| 2026-05-08 evening | Father added the killer requirement: bidirectional awareness. *"after endpoint i enter in loki, it means i am talking to zai there but if i go to telegram and ask him about loki, so he should know and control it."* This is the WiFi-pairing-for-AIs vision. |
| 2026-05-08 evening | This commit — Phase 0 + 1 shipped autonomously. Library + hub + examples + tests + docs + CI + this CLAUDE.md. |

## 5. Phase status

- **Phase 0** ✅ — `zhub.publish()`, hub server, OpenAI-compat /v1/chat/completions proxy, manifest at /<name>/manifest.json, public /registry.
- **Phase 1** ✅ — `zhub.connect()`, capability registration, bidirectional invoke from publisher to connection through the hub. Connection-event delivery to publisher.
- **Phase 0.2 (next)** — Cloudflare Tunnel auto-config so publish works from a laptop without a public IP.
- **Phase 0.3** — Kotlin client lib for Loki Android. JS client for browser/Node.
- **Phase 0.4** — Streaming chat responses end-to-end (SSE through hub).
- **Phase 0.5** — Persistent registry across hub restarts. Web UI for /registry.
- **Phase 1.0** — Signed manifests, hub federation.

## 6. File layout (what's where)

| Concern | Where |
|---|---|
| Public API surface | `zhub/__init__.py` — re-exports `publish`, `connect`, `Manifest`, `Capability`, errors. |
| Manifest schema + builders | `zhub/manifest.py` — `Manifest`, `Capability`, `chat_only_manifest()`. |
| Wire protocol envelopes | `zhub/protocol.py` — `Envelope` + helpers (`register_publisher`, `chat_request`, `invoke_request`, etc.). |
| Hub server (FastAPI + WebSocket) | `zhub/server.py` — `create_app()`, `Hub` class, HTTP routes + `/ws/publish` + `/ws/connect`. |
| Client library (publish + connect) | `zhub/client.py` — `publish()`, `connect()`, `ZhubPublication`, `ZhubConnection`. |
| Custom exceptions | `zhub/errors.py` — `ZhubError`, `AuthError`, `ConnectionError`, `ManifestError`, `CapabilityError`, `HubError`. |
| Examples | `examples/publish_demo.py`, `examples/connect_demo.py`, `examples/orchestrate_demo.py`. |
| Tests | `tests/test_manifest.py`, `tests/test_protocol.py`, `tests/test_e2e.py`. |
| CI | `.github/workflows/ci.yml`. |
| Container | `Dockerfile`. |
| Package config | `pyproject.toml`. |

## 7. Architecture in 4 lines

1. **The hub is a router, not a database.** State lives in the publisher's process and the client's process; the hub multiplexes traffic and holds the in-memory registry.
2. **WebSocket per side.** Each publisher and each connection holds one long-lived WebSocket to the hub.
3. **One Envelope schema** crosses every boundary — `register-publisher`, `chat-request`, `chat-response`, `invoke-request`, `invoke-result`, `connection-event`, `registered`, `error`, `ping`/`pong`.
4. **Bidirectional**: publisher → invoke-request → hub → connection (the AI calls its client's capability). connection → chat-request → hub → publisher (the client calls the AI). Both directions multiplexed on the same WS.

## 8. Landmines

1. **Don't add per-publisher persistence to the hub yet.** v0.1 is in-memory by design. If the hub restarts, publishers re-register and clients reconnect. Persistence is Phase 0.5; doing it now would couple the hub to a specific storage choice prematurely.

2. **API keys are returned to the publisher exactly once** at registration time. The hub stores only the key → AI mapping (raw key in memory; in production, hash before storing). Don't add a "show key" endpoint.

3. **`pub.invoke()` and `conn.chat()` are async — call from inside an event loop.** The orchestration example shows the run-coroutine-threadsafe pattern when calling from a sync chat handler.

4. **Don't break the OpenAI Chat Completions response shape.** External clients use `openai` library expecting the canonical shape. The hub formats publisher responses into `{id, object, created, model, choices: [{index, message: {role, content}, finish_reason}], usage}`.

5. **WebSocket disconnects need graceful handling.** The hub already tears down on `WebSocketDisconnect` and notifies the publisher with `connection-event(disconnected)`. If you change reconnect logic, preserve this.

6. **Father builds tools, not products.** Don't drift into "let's monetize zhub" or "let's add billing". Tool that does one thing well. See `~/.claude/projects/-root/memory/feedback_taste_tool_building.md`.

7. **No commercial framing for Father unless explicitly asked.** No Play Store, no IAP, no pricing decisions. Just tool craftsmanship.

## 9. What's next when Father says go

Mera honest priority order:

1. **Phase 0.2 — Cloudflare Tunnel auto-config.** This is the difference between "demo on localhost" and "actually expose ZAI from Father's phone to the internet." Worth a real day of work. Requires a Cloudflare account + an `cloudflared` binary check on publish.
2. **Phase 0.3 — Kotlin client lib for Loki.** The promised killer demo (Telegram → ZAI → Loki on phone) needs Loki to call `zhub.connect()`. Today only Python clients can. Should be 1-2 days for a clean Kotlin port.
3. **Phase 0.4 — Streaming.** OpenAI-compat clients expect SSE for streaming. Without it, longer responses feel slow.
4. **Phase 0.5 — Persistence.** SQLite-backed registry so hub restarts don't lose state.

Anything Father proposes wins over this default list.

## 10. Father's north star (don't argue, don't drift)

- ZAI eventually runs everywhere — phone, NUC, Father's life. zhub is the connectivity substrate that makes that real.
- Tool craftsmanship over productization. Open source. Anteroom-publishable.
- Bidirectional from day one is the philosophy: the AI is the hub of its own ecosystem.
- Don't replicate ZTerminal mistake — ship things that actually work end-to-end before claiming completion.

## 11. Useful commands at a glance

```bash
# Run hub locally
python -m zhub.server --port 8080

# Run orchestration end-to-end demo
python examples/orchestrate_demo.py

# Test (e2e tests need fastapi/uvicorn)
pip install -e '.[server,dev]'
pytest -v

# Build wheel
pip install build
python -m build

# Run hub in container
docker build -t zhub .
docker run -p 8080:8080 zhub
```

---

**Last updated:** 2026-05-08 (Phase 0+1 ship).
