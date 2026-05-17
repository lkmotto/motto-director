#!/usr/bin/env python3
"""
Scan active_sessions.json, check Factory for completions, report to Fleet.
Safe to run as cron every 30s.

Usage:
    python scripts/poll_completions.py
    python scripts/poll_completions.py --sessions /path/to/active_sessions.json
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Allow running from repo root without install
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from director.completion_handler import CompletionHandler
from director.factory_client import FactoryClient
from director.fleet_client import FleetClient
from director.session_store import SessionStore


def _checkmark(success: bool) -> str:
    return "+" if success else "x"


async def main(sessions_path: str) -> None:
    factory_key = os.environ.get("FACTORY_API_KEY") or os.environ.get(
        "FACTORY_KEY", ""
    )
    mcp_token = os.environ.get("MOTTO_MCP_AUTH_TOKEN", "")

    fleet = FleetClient(auth_token=mcp_token or None)
    factory = FactoryClient(api_key=factory_key or None)
    store = SessionStore(path=sessions_path)

    handler = CompletionHandler(
        fleet_client=fleet,
        factory_client=factory,
        session_store=store,
    )

    running = store.get_running()
    if not running:
        print("No running sessions found.")
        return

    print(f"Checking {len(running)} running session(s)...")
    results = await handler.scan_all()

    if not results:
        print("No completions detected yet.")
        return

    for r in results:
        mark = _checkmark(r.success)
        print(f"[{mark}] {r.session_id[:8]} — {r.summary}")
        if r.error:
            print(f"    error: {r.error}")
        if r.artifact_id:
            print(f"    artifact_id: {r.artifact_id}")

    passed = sum(1 for r in results if r.success)
    print(f"\n{passed}/{len(results)} completed successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Poll Factory sessions for completions")
    parser.add_argument(
        "--sessions",
        default=str(Path(__file__).resolve().parents[1] / "active_sessions.json"),
        help="Path to active_sessions.json",
    )
    args = parser.parse_args()
    asyncio.run(main(args.sessions))
