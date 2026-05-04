"""Entrypoint: perceive → ideate → act loop. Designed for Northflank cron."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime

from director import fleet
from director.act import act
from director.ideate import ideate
from director.observability import event, init_observability, register, track_run
from director.perceive import perceive


def _log(event_name: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event_name,
        **fields,
    }
    print(json.dumps(record, default=str), flush=True)


def run() -> int:
    """Sync entry point for the `motto-director` console script.

    Wraps the async cycle so we can talk to motto-mcp-server (fleet
    coordination) and Langfuse (LLM-aware traces) without changing the
    sync semantics of perceive/ideate/act below. All fleet calls no-op
    gracefully when env vars are unset, so this is safe to ship before
    the MCP server / Neon / Langfuse are provisioned.
    """
    init_observability("motto-director")
    return asyncio.run(_run_async())


async def _run_async() -> int:
    _log("director.start")
    await register()

    async with track_run("perceive_ideate_act_cycle", intent="auto-nudge") as fleet_run:
        # Read the fleet's runtime state BEFORE perceiving our own GitHub
        # view — this is the new "director knows what the other agents
        # have been up to" feed. Returns [] when motto-mcp-server isn't
        # reachable, so the legacy behavior is preserved.
        fleet_status_now = await fleet.fleet_status()
        recent_fleet_events = await fleet.recent_events(since_minutes=30)

        snapshot = perceive()
        _log(
            "director.perceived",
            repos=len(snapshot.repos),
            open_prs=sum(len(r.open_prs) for r in snapshot.repos),
            open_issues=sum(len(r.open_issues) for r in snapshot.repos),
            pipeline_last_status=(
                snapshot.pipeline_auto_nudge.last_run_status
                if snapshot.pipeline_auto_nudge
                else None
            ),
            fleet_agents=len(fleet_status_now),
            fleet_recent_events=len(recent_fleet_events),
        )
        await event(
            "perceived",
            {
                "repos": len(snapshot.repos),
                "open_prs": sum(len(r.open_prs) for r in snapshot.repos),
                "fleet_agents": len(fleet_status_now),
            },
            run=fleet_run,
        )

        moves = ideate(snapshot)
        _log("director.ideated", count=len(moves), moves=[asdict(m) for m in moves])
        await event("ideated", {"moves": len(moves)}, run=fleet_run)

        results = act(moves, snapshot)
        executed = 0
        for r in results:
            _log(
                "director.acted",
                kind=r.move.kind,
                repo=r.move.repo,
                title=r.move.title,
                priority=r.move.priority,
                status=r.status,
                detail=r.detail,
            )
            if r.status == "executed":
                executed += 1

        fleet_run.summary["repos"] = len(snapshot.repos)
        fleet_run.summary["moves"] = len(moves)
        fleet_run.summary["executed"] = executed
        await event(
            "acted",
            {"executed": executed, "total": len(results)},
            run=fleet_run,
        )

        _log("director.done", executed=executed)

    return 0


if __name__ == "__main__":
    sys.exit(run())
