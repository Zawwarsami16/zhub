"""`python -m zhub doctor` — sanity check the install using shipped entity recipes.

Prints what's available, what's missing, and recipes from entity.md
that match the current state. Useful when an install isn't working
and you don't know why.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys


def _yes(label: str, ok: bool, note: str = "") -> None:
    mark = "✓" if ok else "✗"
    suffix = f"  ({note})" if note else ""
    print(f"  {mark} {label}{suffix}")


def run(argv: list[str]) -> None:
    print("zhub doctor — install + environment check\n")

    # 1) Python version
    py = sys.version_info
    _yes(f"python {py.major}.{py.minor}.{py.micro}", py >= (3, 10),
         "need 3.10+" if py < (3, 10) else "")

    # 2) zhub package importable
    try:
        import zhub  # noqa
        _yes("import zhub", True)
    except ImportError as e:
        _yes("import zhub", False, f"err: {e}")
        return

    # 3) [server] extras
    for mod, label in [("fastapi", "fastapi"), ("uvicorn", "uvicorn[standard]"),
                       ("websockets", "websockets")]:
        try:
            importlib.import_module(mod)
            _yes(f"server dep: {label}", True)
        except ImportError:
            _yes(f"server dep: {label}", False,
                 "install: pip install -e '.[server]'")

    # 4) cloudflared
    cf = shutil.which("cloudflared")
    _yes("cloudflared on PATH", bool(cf),
         cf or "optional; install for --public-tunnel / `up` tunneling")

    # 5) brain credentials — derived from the adapter registry so the list
    # can never drift out of sync as adapters are added (see env_keys).
    print("\n  brain availability:")
    try:
        from zhub.brains import REGISTRY, list_available
        seen: set[str] = set()
        for cls in REGISTRY:
            for key in cls.env_keys:
                if key in seen:
                    continue
                seen.add(key)
                v = os.environ.get(key)
                _yes(key, bool(v), "set" if v else "not set (env)")

        avail = list_available()
        if avail:
            _yes("at least one brain detected", True,
                 ", ".join(a.name for a in avail))
        else:
            _yes("at least one brain detected", False,
                 "none reachable")
    except Exception as e:
        _yes("brain detection", False, f"err: {e}")

    # 6) entity surface
    try:
        from pathlib import Path
        ent = Path(zhub.__file__).parent / "entity.md"
        _yes("entity.md ships with package", ent.exists(),
             str(ent) if ent.exists() else "")
    except Exception:
        pass

    print("\n  next steps:")
    print("    python -m zhub up                  # one-command bring-up")
    print("    python -m zhub.server --port 8080  # just the hub")
    print("    curl <hub>/entity                  # what zhub knows about itself")
