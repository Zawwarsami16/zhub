"""Unit tests for PeerRegistry caching (no server, mocked transport).

The server-level test_federation.py exercises the /registry/global wiring;
these cover the cache layer directly, including the regression where a dead
peer was re-fetched on every aggregate() call.
"""

import httpx

from zhub.federation import PeerRegistry


def _registry(peers, handler, *, refresh_seconds=60.0):
    pr = PeerRegistry(peers, refresh_seconds=refresh_seconds)
    pr._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0)
    return pr


async def test_dead_peer_negatively_cached_within_window():
    """A peer that errors is fetched once, then served from cache until the
    refresh window expires — not re-fetched on every call."""
    hits = {"n": 0}

    def handler(request):
        hits["n"] += 1
        raise httpx.ConnectError("refused")

    pr = _registry(["http://dead.example"], handler, refresh_seconds=60.0)
    try:
        for _ in range(3):
            assert await pr.aggregate() == []
        assert hits["n"] == 1
        assert "http://dead.example" in pr._cache
    finally:
        await pr.close()


async def test_dead_peer_retried_after_window_expires():
    """A negatively cached failure is retried once the window lapses."""
    hits = {"n": 0}

    def handler(request):
        hits["n"] += 1
        raise httpx.ConnectError("refused")

    pr = _registry(["http://dead.example"], handler, refresh_seconds=0.0)
    try:
        await pr.get("http://dead.example")
        await pr.get("http://dead.example")
        assert hits["n"] == 2
    finally:
        await pr.close()


async def test_successful_fetch_cached_and_reused():
    """A good registry is fetched once and served from cache while fresh."""
    hits = {"n": 0}
    entries = [{"name": "alpha"}, {"name": "beta"}]

    def handler(request):
        hits["n"] += 1
        return httpx.Response(200, json=entries)

    pr = _registry(["http://peer.example"], handler, refresh_seconds=60.0)
    try:
        assert await pr.get("http://peer.example") == entries
        assert await pr.get("http://peer.example") == entries
        assert hits["n"] == 1
    finally:
        await pr.close()


async def test_non_list_registry_treated_as_empty():
    """A peer returning a non-list body is coerced to an empty registry."""
    def handler(request):
        return httpx.Response(200, json={"not": "a list"})

    pr = _registry(["http://peer.example"], handler)
    try:
        assert await pr.get("http://peer.example") == []
    finally:
        await pr.close()


async def test_non_dict_entries_dropped_from_registry():
    """A peer registry with mixed dict / non-dict entries returns only the
    dict rows — regression for aggregate() crashing at ``dict(e)`` on a
    string/int entry, which would have 500'd /registry/global on the local
    hub and taken its own listings down with it."""
    def handler(request):
        return httpx.Response(200, json=[{"name": "good"}, "junk", 42, None,
                                          {"name": "also-good"}])

    pr = _registry(["http://peer.example"], handler)
    try:
        assert await pr.get("http://peer.example") == [
            {"name": "good"}, {"name": "also-good"},
        ]
        out = await pr.aggregate()
        assert [e["name"] for e in out] == ["good", "also-good"]
        assert all(e["origin"] == "http://peer.example" for e in out)
    finally:
        await pr.close()


async def test_aggregate_annotates_origin():
    """aggregate() tags every peer entry with its origin URL."""
    def handler(request):
        return httpx.Response(200, json=[{"name": "x"}])

    pr = _registry(["http://a.example", "http://b.example"], handler)
    try:
        out = await pr.aggregate()
        assert {e["origin"] for e in out} == {"http://a.example", "http://b.example"}
        assert all(e["name"] == "x" for e in out)
    finally:
        await pr.close()


async def test_aggregate_no_peers_returns_empty():
    def handler(request):  # never called
        raise AssertionError("should not fetch with no peers")

    pr = _registry([], handler)
    try:
        assert await pr.aggregate() == []
    finally:
        await pr.close()


async def test_registry_url_strips_trailing_slash():
    """The /registry path is appended without doubling the slash."""
    seen = {"url": None}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[])

    pr = _registry(["http://peer.example/"], handler)
    try:
        await pr.get("http://peer.example/")
        assert seen["url"] == "http://peer.example/registry"
    finally:
        await pr.close()
