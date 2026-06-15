"""Lightweight, read-only federation between hubs.

A hub can be configured with a list of peer hub URLs. Periodically, each
peer's `/registry` is fetched and cached. The aggregator endpoint
`/registry/global` returns local listings + peer listings, annotated with
their origin URL.

This is one-hop discovery only:
  - no cross-hub call routing (clients still go through their hub's own
    publishers)
  - no shared registry state
  - offline peers are skipped silently — never block the local response

Anything richer (cross-hub call routing, signed peer relationships, etc.)
is Phase 1.1+.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

try:
    import httpx
except ImportError as e:
    raise SystemExit(
        "zhub.federation requires httpx. install:\n"
        "    pip install httpx"
    ) from e


log = logging.getLogger("zhub.federation")


class PeerRegistry:
    """Caches peer hub registries with a refresh interval."""

    def __init__(self, peers: list[str], refresh_seconds: float = 60.0,
                 timeout_seconds: float = 5.0) -> None:
        self.peers = peers
        self.refresh_seconds = refresh_seconds
        self._cache: dict[str, tuple[list[dict[str, Any]], float]] = {}
        self._http = httpx.AsyncClient(timeout=timeout_seconds)

    async def get(self, peer_url: str) -> list[dict[str, Any]]:
        """Return cached peer registry if fresh, otherwise re-fetch.
        Empty list on any failure — callers must not block on a dead peer."""
        cached = self._cache.get(peer_url)
        if cached and time.time() - cached[1] < self.refresh_seconds:
            return cached[0]
        try:
            resp = await self._http.get(peer_url.rstrip("/") + "/registry")
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                log.warning("peer %s returned non-list registry; treating as empty", peer_url)
                data = []
        except Exception as e:
            log.warning("peer %s unreachable: %s", peer_url, e)
            # Negatively cache the failure: without this a dead peer is
            # re-fetched on every aggregate() call, paying the full request
            # timeout each time and hammering the down peer — the
            # refresh_seconds throttle only ever protected successful fetches.
            # An empty entry is retried once the window expires, same as a
            # stale success.
            self._cache[peer_url] = ([], time.time())
            return []
        self._cache[peer_url] = (data, time.time())
        return data

    async def aggregate(self) -> list[dict[str, Any]]:
        """Return all peer entries annotated with their origin URL.
        Concurrent fetch — slow peers don't block fast ones."""
        if not self.peers:
            return []
        results = await asyncio.gather(
            *(self.get(peer) for peer in self.peers),
            return_exceptions=False,
        )
        out: list[dict[str, Any]] = []
        for peer, entries in zip(self.peers, results):
            for e in entries:
                e2 = dict(e)
                e2["origin"] = peer
                out.append(e2)
        return out

    async def close(self) -> None:
        await self._http.aclose()
