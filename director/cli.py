"""Subcommand dispatcher for the Northflank cron migration.

Northflank cron jobs each run one process, which means we need a CLI surface
that maps a subcommand name onto an existing director entry point. This
module is a thin shim — every subcommand calls into a function that already
exists (main.run, digest.run_digest, meta.run_meta).

Usage:
    python -m director.cli cycle      # full perceive → ideate → act loop
    python -m director.cli perceive   # alias for cycle (see note below)
    python -m director.cli ideate     # alias for cycle
    python -m director.cli act        # alias for cycle
    python -m director.cli digest     # build + send morning Telegram digest
    python -m director.cli meta       # weekly self-improvement cron

Why perceive/ideate/act are aliases for cycle (not independent processes):
the existing pipeline passes a Snapshot from perceive() into ideate() and a
list of NextMove into act() in-process. Splitting them into separate
processes would require state to flow through Neon (which is doable, but
out of scope for the heartbeat migration). The Northflank YAML registers
three separate crons so we have three named heartbeats with different
cadences; each one runs the full cycle today, and a future PR can split
them once the Neon state-passing path lands.
"""

from __future__ import annotations
import sys as _sys, pathlib as _pathlib  # noqa: E402
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
import sentry_init  # noqa: E402,F401

import sys


def _print_usage() -> int:
    print(
        "usage: python -m director.cli "
        "<cycle|perceive|ideate|act|digest|meta>",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        return _print_usage()
    cmd = args[0].lower()

    if cmd in ("cycle", "perceive", "ideate", "act"):
        from director.main import run

        return run()
    if cmd == "digest":
        from director.digest import run_digest

        return run_digest()
    if cmd == "meta":
        from director.meta import run_meta

        return run_meta()
    return _print_usage()


if __name__ == "__main__":
    import sentry_sdk as _sentry_sdk
    try:
        sys.exit(main())
    except Exception as _exc:
        _sentry_sdk.capture_exception(_exc)
        raise

