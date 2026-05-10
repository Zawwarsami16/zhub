"""Phase 10.0 — per-AI latency percentiles in /metrics.

Hub maintains a small ring buffer (last 200 latencies) per AI; /metrics
exposes p50, p95, p99 computed on demand. Tail-latency visibility for
operators — avg + max alone hides the actual pain.
"""

import asyncio
import socket
import threading
import time

import pytest

try:
    import fastapi  # noqa
    import uvicorn  # noqa
    import httpx  # noqa
    DEPS_AVAILABLE = True
except ImportError:
    DEPS_AVAILABLE = False

if DEPS_AVAILABLE:
    from zhub.server import create_app, Hub
from zhub import publish


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


# --- pure unit tests on Hub.percentile() helper ---------------------------

def test_percentile_empty_returns_zero():
    h = Hub()
    assert h.compute_percentile("nope", 50) == 0


def test_percentile_p50_p95_p99_against_known_distribution():
    h = Hub()
    # Inject 100 sorted-ish samples 1..100ms
    for ms in range(1, 101):
        h.record_latency("ai", float(ms))
    p50 = h.compute_percentile("ai", 50)
    p95 = h.compute_percentile("ai", 95)
    p99 = h.compute_percentile("ai", 99)
    # Standard nearest-rank: p50 ≈ 50, p95 ≈ 95, p99 ≈ 99 (give ±1 leeway)
    assert 49 <= p50 <= 51, f"p50={p50}"
    assert 94 <= p95 <= 96, f"p95={p95}"
    assert 98 <= p99 <= 100, f"p99={p99}"


def test_latency_ring_buffer_bounded():
    """Only the last 200 latencies are kept — older ones get pushed out
    so the percentile reflects recent behavior, not lifetime history."""
    h = Hub()
    # 100 small samples (1ms), then 200 large samples (1000ms)
    for _ in range(100):
        h.record_latency("ai", 1.0)
    for _ in range(200):
        h.record_latency("ai", 1000.0)
    # Older 1ms samples should be evicted; p50 should be near 1000
    p50 = h.compute_percentile("ai", 50)
    assert 900 <= p50 <= 1100, f"p50={p50} (ring buffer should have evicted small samples)"


# --- end-to-end: percentiles surface in /metrics + /api/dashboard ---------

@pytest.fixture
def hub():
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    port = _free_port()
    app = create_app()

    def run():
        config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                log_level="warning")
        asyncio.run(uvicorn.Server(config).serve())

    threading.Thread(target=run, daemon=True).start()
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)
    yield port


@pytest.mark.asyncio
async def test_metrics_includes_percentiles_per_ai(hub):
    pub = publish(
        name="pct-bot", description="x",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        for _ in range(5):
            await c.post(
                f"http://127.0.0.1:{hub}/{pub.name}/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "x"}]},
                headers={"Authorization": f"Bearer {pub.api_key}"},
            )
        m = (await c.get(f"http://127.0.0.1:{hub}/metrics")).json()

    by_ai = m["by_ai"]
    assert pub.name in by_ai
    e = by_ai[pub.name]
    for k in ("p50_latency_ms", "p95_latency_ms", "p99_latency_ms"):
        assert k in e, f"missing {k!r}: {e!r}"
        assert isinstance(e[k], int)


@pytest.mark.asyncio
async def test_dashboard_includes_percentiles(hub):
    pub = publish(
        name="dash-pct-bot", description="x",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        for _ in range(3):
            await c.post(
                f"http://127.0.0.1:{hub}/{pub.name}/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "x"}]},
                headers={"Authorization": f"Bearer {pub.api_key}"},
            )
        d = (await c.get(f"http://127.0.0.1:{hub}/api/dashboard")).json()

    by_ai = d["by_ai"]
    assert pub.name in by_ai
    e = by_ai[pub.name]
    for k in ("p50_latency_ms", "p95_latency_ms", "p99_latency_ms"):
        assert k in e
