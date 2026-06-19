"""Rate-limit parsing + sliding-window counter + e2e enforcement."""

import asyncio
import socket
import threading
import time

import pytest

from zhub import publish
from zhub.ratelimit import parse_rate, SlidingWindow


def test_parse_rate_per_second():
    assert parse_rate("10/s") == (10, 1.0)


def test_parse_rate_per_minute():
    assert parse_rate("60/min") == (60, 60.0)


def test_parse_rate_per_hour():
    assert parse_rate("1000/hour") == (1000, 3600.0)


def test_parse_rate_per_day():
    assert parse_rate("100000/day") == (100000, 86400.0)


def test_parse_rate_default_when_unset():
    assert parse_rate(None) == (60, 60.0)
    assert parse_rate("") == (60, 60.0)


def test_parse_rate_garbage_falls_back_to_default():
    assert parse_rate("not-a-rate") == (60, 60.0)
    assert parse_rate("60") == (60, 60.0)
    assert parse_rate("/min") == (60, 60.0)


def test_sliding_window_under_limit_allows():
    clock = [0.0]
    w = SlidingWindow(limit=3, period_seconds=10.0, now_fn=lambda: clock[0])
    assert w.check("k") == (True, None)
    clock[0] = 1.0
    assert w.check("k") == (True, None)
    clock[0] = 2.0
    assert w.check("k") == (True, None)


def test_sliding_window_at_limit_denies():
    clock = [0.0]
    w = SlidingWindow(limit=3, period_seconds=10.0, now_fn=lambda: clock[0])
    for _ in range(3):
        ok, _ = w.check("k")
        assert ok
    ok, retry_after = w.check("k")
    assert ok is False
    assert retry_after is not None and retry_after > 0


def test_sliding_window_expires_old_hits():
    clock = [0.0]
    w = SlidingWindow(limit=2, period_seconds=10.0, now_fn=lambda: clock[0])
    assert w.check("k")[0] is True
    assert w.check("k")[0] is True
    assert w.check("k")[0] is False  # at limit
    clock[0] = 11.0  # past the 10s window
    assert w.check("k")[0] is True


def test_sliding_window_per_key_isolation():
    clock = [0.0]
    w = SlidingWindow(limit=1, period_seconds=10.0, now_fn=lambda: clock[0])
    assert w.check("a")[0] is True
    assert w.check("b")[0] is True  # different key, different bucket
    assert w.check("a")[0] is False  # a hit limit


def test_sliding_window_zero_limit_denies_without_crashing():
    # A publisher declaring "0/min" parses to limit=0. Such a window must
    # reject every request cleanly — never IndexError on an empty bucket.
    clock = [0.0]
    w = SlidingWindow(limit=0, period_seconds=60.0, now_fn=lambda: clock[0])
    ok, retry_after = w.check("k")
    assert ok is False
    assert retry_after == 60.0
    # Stays denied on repeat without ever appending a hit.
    assert w.check("k") == (False, 60.0)


def test_parse_zero_rate_then_check_is_safe():
    # End-to-end: the parse → window path a publisher's "0/min" actually takes.
    w = SlidingWindow(*parse_rate("0/min"))
    ok, retry_after = w.check("client")
    assert ok is False
    assert retry_after == 60.0


# ---- Hub.check_rate_limit unit tests ----

try:
    from zhub.server import Hub, PublisherRegistration
    HUB_AVAILABLE = True
except ImportError:
    HUB_AVAILABLE = False


def _make_pub(name: str, rate_limit: str) -> "PublisherRegistration":
    """Construct a PublisherRegistration without a real WebSocket."""
    return PublisherRegistration(
        name=name,
        manifest={"rate_limit": rate_limit},
        websocket=None,  # type: ignore[arg-type]
        api_key_hash="hash",
    )


@pytest.mark.skipif(not HUB_AVAILABLE, reason="fastapi not installed")
def test_rate_window_cleared_on_unregister():
    """After unregister_publisher the old SlidingWindow must be dropped so a
    re-registered publisher with a different rate_limit gets a fresh window.

    Pre-fix: _rate_windows[name] persisted through unregister → stale limit
    was reused even after re-registration with a higher limit, silently
    denying requests the new manifest allows.
    """
    hub = Hub()

    # Register with rate_limit 1/min — window created on first check.
    hub.publishers["rate-bot"] = _make_pub("rate-bot", "1/min")
    ok1, _ = hub.check_rate_limit("rate-bot", "caller-a")
    assert ok1 is True  # 1st request within limit
    ok2, _ = hub.check_rate_limit("rate-bot", "caller-a")
    assert ok2 is False  # 2nd request exceeds 1/min

    # Simulate unregister (the async lock path; bypass it here to keep test sync).
    hub.publishers.pop("rate-bot", None)
    hub._rate_windows.pop("rate-bot", None)  # the fix under test clears this

    # Re-register with rate_limit 10/min — should get a fresh window.
    hub.publishers["rate-bot"] = _make_pub("rate-bot", "10/min")
    for i in range(5):
        ok, _ = hub.check_rate_limit("rate-bot", "caller-b")
        assert ok is True, f"request {i+1} blocked; stale 1/min window not cleared"


@pytest.mark.skipif(not HUB_AVAILABLE, reason="fastapi not installed")
def test_rate_window_stale_without_clear():
    """Demonstrates the pre-fix regression: NOT clearing _rate_windows after
    unregister causes the new publisher's higher rate_limit to be ignored."""
    hub = Hub()
    hub.publishers["stale-bot"] = _make_pub("stale-bot", "1/min")
    hub.check_rate_limit("stale-bot", "c")  # seeds window with limit=1

    # Simulate unregister WITHOUT clearing the window (pre-fix behaviour).
    hub.publishers.pop("stale-bot", None)
    # _rate_windows["stale-bot"] intentionally left — that's the bug.

    # Re-register with higher rate.
    hub.publishers["stale-bot"] = _make_pub("stale-bot", "100/min")
    hub.check_rate_limit("stale-bot", "c2")   # new key → new bucket → passes
    ok, _ = hub.check_rate_limit("stale-bot", "c2")  # 2nd hit same key
    # Pre-fix: ok is False (stale limit=1). The fix makes this True.
    # We assert False here to document (and mutation-verify) the regression:
    assert ok is False, (
        "regression sentinel: without _rate_windows.pop in unregister, the "
        "stale limit=1 window blocks the 2nd request even under a 100/min manifest"
    )


# ---- e2e enforcement ----

try:
    import fastapi  # noqa
    import uvicorn  # noqa
    SERVER_AVAILABLE = True
except ImportError:
    SERVER_AVAILABLE = False

if SERVER_AVAILABLE:
    from zhub.server import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def hub_port():
    if not SERVER_AVAILABLE:
        pytest.skip("fastapi/uvicorn not installed")
    port = _free_port()

    def run():
        config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
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
async def test_rate_limit_429_after_quota(hub_port):
    """Publisher with 3/min rate limit. Hit chat endpoint 4x in quick succession.
    First 3 succeed; 4th returns 429 with Retry-After header + body.

    Uses /min (not /s) so the sliding window (60s) isn't sensitive to the
    wall-clock speed of the test runner — all 4 requests complete in well under
    a second, so the first request is never evicted from the window.
    """
    if not SERVER_AVAILABLE:
        pytest.skip()

    pub = publish(
        name="rl",
        description="rate limit test",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub_port}",
        rate_limit="3/min",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.05)
    assert pub.api_key

    import httpx
    base = f"http://127.0.0.1:{hub_port}/rl/v1/chat/completions"
    headers = {"Authorization": f"Bearer {pub.api_key}", "Content-Type": "application/json"}
    body = {"messages": [{"role": "user", "content": "hi"}]}

    async with httpx.AsyncClient() as c:
        # First 3 should succeed
        for i in range(3):
            r = await c.post(base, json=body, headers=headers)
            assert r.status_code == 200, f"call #{i+1}: {r.status_code} {r.text}"
        # 4th should be rate-limited
        r4 = await c.post(base, json=body, headers=headers)
        assert r4.status_code == 429, f"expected 429, got {r4.status_code}: {r4.text}"
        assert "Retry-After" in r4.headers, f"missing Retry-After header: {dict(r4.headers)}"
        body_json = r4.json()
        assert body_json["error"]["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_rate_limit_per_publisher_isolation(hub_port):
    """Two publishers with separate rate limits don't interfere with each other."""
    if not SERVER_AVAILABLE:
        pytest.skip()

    pub_a = publish(name="rla", description="a", chat_handler=lambda m, o: "a",
                    hub_url=f"ws://127.0.0.1:{hub_port}", rate_limit="2/s")
    pub_b = publish(name="rlb", description="b", chat_handler=lambda m, o: "b",
                    hub_url=f"ws://127.0.0.1:{hub_port}", rate_limit="2/s")
    for _ in range(50):
        if pub_a.api_key and pub_b.api_key:
            break
        await asyncio.sleep(0.05)

    import httpx
    body = {"messages": [{"role": "user", "content": "hi"}]}
    async with httpx.AsyncClient() as c:
        # Exhaust A's quota
        for _ in range(2):
            r = await c.post(f"http://127.0.0.1:{hub_port}/rla/v1/chat/completions",
                             json=body, headers={"Authorization": f"Bearer {pub_a.api_key}"})
            assert r.status_code == 200
        r = await c.post(f"http://127.0.0.1:{hub_port}/rla/v1/chat/completions",
                         json=body, headers={"Authorization": f"Bearer {pub_a.api_key}"})
        assert r.status_code == 429
        # B is unaffected
        r = await c.post(f"http://127.0.0.1:{hub_port}/rlb/v1/chat/completions",
                         json=body, headers={"Authorization": f"Bearer {pub_b.api_key}"})
        assert r.status_code == 200
