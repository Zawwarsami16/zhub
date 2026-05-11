# Contributing to zhub

zhub is intentionally small: each file does one thing, the test bar is high, the public surface stays neutral. Keeping it that way is the contribution.

## Setup

```bash
git clone https://github.com/Zawwarsami16/zhub
cd zhub
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[server,dev,brains]'
python -m zhub doctor   # verify install
pytest                   # 164 tests, ~80s
```

For the JS client:

```bash
cd js
npm install
npm test                  # 13 tests
```

## How to propose a change

| Change type | Path |
|---|---|
| Bugfix or small refactor | open a PR with the diff + a regression test |
| New feature, brain adapter, primitive | open an issue first (or a `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md` PR) so the design is reviewable before implementation |
| Documentation | direct PR is fine |

## Code rules

- **Test-driven.** Every behavior change ships with a test. If you add a new file under `zhub/`, there's a matching `tests/test_<name>.py`.
- **Substrate, not opinion.** zhub knows nothing about your specific AI, your devices, or your business logic. Don't add product-specific bridges — those go in their own repos and consume zhub via `publish()` / `connect()` / `expose()`.
- **No new top-level deps without justification.** The optional-extras pattern (`[server]`, `[crypto]`, `[brains]`) handles most cases. Hot path stays import-light.
- **Async-first.** Hub-side code is asyncio. Sync work belongs in `to_thread` or its own subprocess.
- **One-line comments only when the *why* is non-obvious.** Self-documenting names beat narration.

## Adding a brain adapter

Most hosted LLM providers speak OpenAI-compat. Use the shared helper:

```python
# zhub/brains/<provider>.py
from ._openai_compat import probe_openai_compat, stream_openai_compat
from .base import BrainAdapter, ChatChunk
import httpx, os

_BASE_URL = "https://api.<provider>.com/v1"
_DEFAULT_MODEL = "..."

class <Provider>Adapter(BrainAdapter):
    name = "<provider>"
    label = "<Display Name>"
    # ... __init__, try_init using probe_openai_compat, stream delegating to stream_openai_compat
```

Then register in `zhub/brains/__init__.py` REGISTRY (priority order matters for `detect()`) and add `tests/test_brains_<provider>.py` with the same monkeypatch + fake-stream pattern as the existing adapter tests.

For non-OpenAI shapes (e.g. Cohere), copy `zhub/brains/cohere.py` as a template.

## Running the live demo against your branch

```bash
GROQ_API_KEY=gsk_... python -m zhub up
# → URL + KEY
```

Open `http://localhost:8080/` in a browser to see the dashboard. Generate traffic with:

```bash
curl -s http://localhost:8080/healthz
curl -s -X POST http://localhost:8080/me/v1/chat/completions \
     -H "Authorization: Bearer zk_..." \
     -d '{"messages":[{"role":"user","content":"hi"}]}'
```

Particles fly through the live SVG flow on the dashboard for each request.

## Where the design conversations live

`docs/superpowers/specs/` has the design doc for every shipped phase. `docs/superpowers/plans/` has the TDD implementation plans. Walking through a few specs is the fastest way to understand how decisions were made.

## License

By contributing, you agree your contributions will be licensed under the MIT License.
