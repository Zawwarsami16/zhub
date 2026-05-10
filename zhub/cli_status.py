"""`python -m zhub status <hub-url>` — pretty-print remote hub state.

Useful operator tool. Hits `<url>/api/dashboard` (no auth needed; the
endpoint is operator-public anyway) and prints publishers, exposures,
recent activity, latency percentiles, peers — all in a terminal-friendly
table format. Same data the browser dashboard polls.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

try:
    import httpx
except ImportError as e:
    raise SystemExit(
        "zhub status requires httpx. install:\n"
        "    pip install httpx"
    ) from e


def _fmt_uptime(s: int) -> str:
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def _print_table(rows: list[list[str]], headers: list[str]) -> None:
    widths = [
        max(len(headers[i]), max((len(row[i]) for row in rows), default=0))
        for i in range(len(headers))
    ]
    sep = "  "
    print(sep.join(h.ljust(w) for h, w in zip(headers, widths)))
    print(sep.join("-" * w for w in widths))
    for row in rows:
        print(sep.join(c.ljust(w) for c, w in zip(row, widths)))


def _render(d: dict[str, Any]) -> None:
    print()
    print(f"  hub_id:      {d['hub_id']}")
    print(f"  uptime:      {_fmt_uptime(d['uptime_seconds'])}")
    print(f"  publishers:  {len(d['publishers'])}")
    print(f"  connections: {d['connections_total']}")
    print(f"  exposures:   {len(d['exposures'])}")
    print(f"  peers:       {len(d['peers'])}")
    print()

    if d["publishers"]:
        print("  PUBLISHERS")
        rows = []
        for p in d["publishers"]:
            m = d["by_ai"].get(p["name"], {})
            vis = "public" if p["public"] else "private"
            rows.append([
                p["name"],
                vis,
                str(m.get("chat_requests", 0)),
                f"{m.get('avg_latency_ms', 0)}/{m.get('p95_latency_ms', 0)}/{m.get('max_latency_ms', 0)}",
                str(p["connections"]),
                _fmt_uptime(p["uptime_seconds"]),
            ])
        _print_table(rows, ["name", "vis", "chats", "avg/p95/max ms", "conn", "uptime"])
        print()

    if d["exposures"]:
        print("  EXPOSURES")
        rows = []
        for e in d["exposures"]:
            vis = "discoverable" if e["public"] else "private"
            caps = ", ".join(e["capabilities"]) or "—"
            rows.append([
                e["name"], vis, caps, _fmt_uptime(e["uptime_seconds"]),
                e["exposure_id"][:14] + "…",
            ])
        _print_table(rows, ["name", "vis", "capabilities", "uptime", "id"])
        print()

    if d["recent_requests"]:
        print(f"  RECENT REQUESTS ({len(d['recent_requests'])} of last 50)")
        # newest 8
        for r in reversed(d["recent_requests"][-8:]):
            ai = f" ai={r['ai']}" if r["ai"] else ""
            print(f"    {r['status']} {r['method']:5} {r['path']:46} "
                  f"{r['latency_ms']:>5}ms{ai}")
        print()

    if d["peers"]:
        print("  PEERS")
        for p in d["peers"]:
            print(f"    {p}")
        print()


def run(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m zhub status",
        description="Pretty-print a remote zhub hub's state.",
    )
    parser.add_argument("url",
                        help="hub base URL (e.g. https://hub.example.com)")
    parser.add_argument("--json", action="store_true",
                        help="emit raw JSON instead of formatted tables")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="request timeout in seconds (default 5)")
    args = parser.parse_args(argv)

    base = args.url.rstrip("/")
    try:
        r = httpx.get(f"{base}/api/dashboard", timeout=args.timeout)
        r.raise_for_status()
        d = r.json()
    except httpx.HTTPError as e:
        print(f"error: could not reach {base}/api/dashboard: {e}",
              file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(d, indent=2))
    else:
        _render(d)
