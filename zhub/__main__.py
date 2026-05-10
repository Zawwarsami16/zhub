"""`python -m zhub` — top-level CLI dispatcher.

Subcommands:
    up        one-shot: hub + (optional) tunnel + brain publisher; prints URL/key
    server    same as `python -m zhub.server` (for backwards compat)
    doctor    inspect the current install + entity recipes for common issues

Default `python -m zhub` (no args) prints the usage line.
"""

from __future__ import annotations

import sys


_USAGE = """\
usage: python -m zhub <command> [options]

commands:
  up         start hub + tunnel + brain publisher in one go (recommended)
  server     start just the hub server (legacy entry point)
  doctor     diagnose the install using shipped entity recipes
  status     pretty-print a remote hub's state from its /api/dashboard

run `python -m zhub <command> --help` for command-specific options.
"""


def main(argv: list[str] | None = None) -> None:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in ("-h", "--help"):
        print(_USAGE)
        return
    cmd, rest = args[0], args[1:]
    if cmd == "server":
        # Defer to the existing server entry point with the rest of argv.
        sys.argv = ["zhub.server"] + rest
        from zhub.server import main as server_main
        server_main()
        return
    if cmd == "up":
        from zhub.cli_up import run as up_run
        up_run(rest)
        return
    if cmd == "doctor":
        from zhub.cli_doctor import run as doctor_run
        doctor_run(rest)
        return
    if cmd == "status":
        from zhub.cli_status import run as status_run
        status_run(rest)
        return
    print(f"unknown command: {cmd!r}\n", file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
