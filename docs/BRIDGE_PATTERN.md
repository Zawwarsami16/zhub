# Bridge pattern — wrap an interactive AI as a zhub publisher

zhub publishers normally have a `chat_handler(messages, options) -> str`
that returns a reply directly — fine when the brain is an HTTP API
(Groq, Ollama, Anthropic, …) you can call inline.

The **bridge pattern** is for when the brain is *interactive* — a Claude
Code session, a Cursor tab, an agent loop with human-in-the-loop, an
in-process model with no HTTP surface, even just a person at a keyboard.
The handler can't reply inline because the actual replier is somewhere
else.

The fix: serialize each request to a file in an `inbox/` directory; the
external session watches that directory, reads the request, writes the
reply to a matching `outbox/` file. The handler polls outbox until the
reply lands (or times out), then returns it like any normal chat.

```
                       hub
                        │
                        ▼
chat request ─► chat_handler() ─► writes inbox/<id>.json
                                    │
                                    │ (other process / agent)
                                    ▼
                            reads inbox/<id>.json
                            formulates a reply
                            writes outbox/<id>.json
                                    │
                       chat_handler reads outbox ──► returns reply
                                    │
                                    ▼
                              hub responds
```

## When to use it

| Use case | Bridge? |
|---|---|
| Reach a Claude Code session running on your laptop from your phone | ✅ |
| Demo / debugging — see exactly what messages your hub is receiving | ✅ |
| Wrap a custom in-process LLM (no HTTP API) | ✅ |
| Wrap an agent that needs a human to approve each reply | ✅ |
| Cheap fast chat with Groq / Anthropic / Ollama | ❌ — use `multi_brain_publisher.py` |
| Production traffic | ❌ — files don't scale; use a real brain |

## Latency budget

| Component | Typical |
|---|---|
| File-bridge polling overhead | ~0.4s (configurable) |
| External agent thinking time | depends on the agent (Claude Sonnet ~0.5–3s, Opus ~2–10s, human ~∞) |
| End-to-end | dominated by the agent, not the bridge |

The bridge is **not** trying to be fast. It exists to give an interactive
agent the same OpenAI-compat surface that a normal brain has, so any
client (Pocket, curl, Claude Desktop, Cursor) can talk to it.

## Cost

The bridge itself costs nothing — just file I/O. The external agent
costs whatever it costs to run. A bridged Claude Sonnet session is
~10× cheaper than a bridged Opus session per reply, so pick the model
that matches what you're actually doing on the other side.

## Recipe

1. **Start a hub:** `python -m zhub.server --port 8080`
2. **Start the bridge publisher:**
   ```bash
   python examples/session_bridge_publisher.py --name session
   ```
   Note the printed URL + KEY. The script also prints the inbox/outbox
   paths (default `/tmp/zhub-inbox/` and `/tmp/zhub-outbox/`).
3. **Point the agent at the inbox.** For Claude Code, in another window:
   > "Watch `/tmp/zhub-inbox/` for new `.json` files. For each, read the
   > `messages` array, formulate a reply, and write `{"text": "..."}` to
   > the matching path under `/tmp/zhub-outbox/`. Stay running and keep
   > processing new files."

   For a script, see `_example_auto_watcher` in
   `examples/session_bridge_publisher.py`.
4. **Use the URL+KEY from anywhere** — Pocket, curl, Cursor, Claude
   Desktop. Each chat round-trips through the file bridge to whoever
   is watching.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Client times out | Watcher not running, or agent took >600s | Start the watcher; bump `--timeout` |
| Replies look stale | Watcher is replaying old inbox files | Clear `/tmp/zhub-inbox/*.json` between sessions |
| Replies are empty | Outbox JSON missing `"text"` field | Watcher must write `{"text": "..."}` exactly |
| Two watchers, double replies | Race | Run only one watcher per inbox |

## Why this exists

I wrote this the first time because I wanted to talk to a Claude Code
session on my laptop from Pocket on my phone. The original lived in
`/tmp/` as throwaway. After it kept working I moved it into the repo
so I (and anyone else) wouldn't have to redo the trick from scratch
every time.
