#!/usr/bin/env python3
"""Integration test: run one full perceive->ideate->act cycle and print what happened.

Usage:
    python scripts/test_cycle.py             # live run (spawns real droids)
    python scripts/test_cycle.py --dry-run   # skip ideate/act, print perception only

Exits 0 on success, 1 on cycle error.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Allow imports from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from director.factory_client import FactoryClient
from director.fleet_client import FleetClient
from director.goals import GoalStore
from director.perceive import perceive
from director.ideate import ideate
from director.session_store import SessionStore


def _print_section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print('=' * 60)


async def test_cycle(dry_run: bool = False) -> bool:
    print("\nmotto-director test cycle")
    print(f"  ONA_FLEET_URL     : {os.getenv('ONA_FLEET_URL') or os.getenv('MOTTO_MCP_URL', '(not set)')}")
    print(f"  FACTORY_API_KEY   : {'set' if os.getenv('FACTORY_API_KEY') else 'NOT SET'}")
    print(f"  ANTHROPIC_API_KEY : {'set' if os.getenv('ANTHROPIC_API_KEY') else 'NOT SET'}")
    print(f"  FACTORY_COMPUTER_ID: {os.getenv('FACTORY_COMPUTER_ID', '(not set)')}")
    print(f"  dry_run           : {dry_run}")

    fleet = FleetClient()
    factory = FactoryClient()
    sessions = SessionStore()
    goals = GoalStore()

    # --- Perception ---
    _print_section("1. PERCEIVE — Fleet state")
    try:
        perception = await perceive(fleet, sessions, goals)
        print(f"  Fleet agents      : {len(perception.fleet_agents)}")
        for a in perception.fleet_agents:
            print(f"    - {a.get('name', '?')}  last_seen={str(a.get('last_seen_at', '?'))[:19]}")
        print(f"  Recent events     : {len(perception.recent_events)}")
        if perception.recent_events:
            for e in perception.recent_events[-3:]:
                print(f"    - [{e.get('level','info')}] {e.get('agent_name','?')}: {e.get('kind','?')}")
        print(f"  Open intents      : {len(perception.open_intents)}")
        print(f"  Active sessions   : {len(perception.active_sessions)}")
        if perception.active_sessions:
            for s in perception.active_sessions:
                print(f"    {s.session_id[:16]}...  goal={s.goal_id}  status={s.status}")
        print(f"  Active goals      : {len(perception.active_goals)}")
        for g in perception.active_goals:
            running = any(
                s.goal_id == g['id'] and s.status == 'running'
                for s in perception.active_sessions
            )
            flag = "[DROID RUNNING]" if running else "[OPEN]"
            print(f"    {flag} {g['id']}: {g['title']}")
    except Exception as exc:
        print(f"  ERROR in perceive: {exc}")
        return False

    if dry_run:
        print("\n  --dry-run: skipping ideate and act.")
        print("  Perception complete. Set env vars and remove --dry-run for a live cycle.")
        return True

    # --- Ideation ---
    _print_section("2. IDEATE — Task proposals")
    try:
        max_droids = int(os.getenv('MAX_PARALLEL_DROIDS', '5'))
        tasks = await ideate(perception, max_droids=max_droids)
        print(f"  Tasks proposed    : {len(tasks)}")
        for t in tasks:
            print(f"    goal={t.get('goal_id')}  repo={t.get('repo')}  title={t.get('task_title')}")
            print(f"      prompt preview: {t.get('prompt','')[:120]}...")
    except Exception as exc:
        print(f"  ERROR in ideate: {exc}")
        return False

    # --- Act (print only, skip actual spawning in test) ---
    _print_section("3. ACT — What would be spawned")
    if not tasks:
        print("  No tasks to spawn (all goals covered or no active goals).")
    else:
        print(f"  Would spawn {len(tasks)} Factory droid(s):")
        for t in tasks:
            print(f"    - goal={t.get('goal_id')} repo={t.get('repo')}")
            print(f"      title: {t.get('task_title')}")
        print()
        confirm = input("  Proceed with live spawn? [y/N] ").strip().lower()
        if confirm == 'y':
            from director.act import act
            await act(tasks, fleet, factory, sessions, goals)
            print("  Spawned. Check active_sessions.json for session IDs.")
        else:
            print("  Skipped. Run `python main.py --once` to execute a full live cycle.")

    _print_section("4. SUMMARY")
    print(json.dumps({
        "fleet_agents": len(perception.fleet_agents),
        "active_goals": len(perception.active_goals),
        "active_sessions": len(perception.active_sessions),
        "open_intents": len(perception.open_intents),
        "tasks_proposed": len(tasks),
    }, indent=2))

    return True


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Run one director cycle and print results")
    parser.add_argument("--dry-run", action="store_true", help="perceive only, skip ideate/act")
    args = parser.parse_args()

    ok = asyncio.run(test_cycle(dry_run=args.dry_run))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
