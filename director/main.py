"""Entrypoint: perceive → ideate → act loop. Designed for Northflank cron."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime

from director import fleet, policy
from director.act import act, fleet_run_id_var
from director.concurrency import adaptive_session_limit
from director.ideate import ideate
from director.observability import event, init_observability, register, track_run
from director.perceive import perceive

logger = logging.getLogger(__name__)

CYCLE_LOCK_RESOURCE = "director:cycle"
CYCLE_LOCK_TTL_SECONDS = 900  # 15 minutes — cron interval is 15-30 min


def _log(event_name: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event_name,
        **fields,
    }
    print(json.dumps(record, default=str), flush=True)


def _neon_dsn() -> str | None:
    return os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")


def _try_acquire_cycle_lock(holder: str) -> bool:
    """Best-effort fleet lock against a Neon `locks` table. No-ops + returns
    True when Neon isn't configured so the legacy single-cron behavior is
    preserved.

    Schema assumed (control plane is already provisioned):
        locks(resource TEXT PRIMARY KEY, holder TEXT, expires_at TIMESTAMPTZ)
    """
    dsn = _neon_dsn()
    if not dsn:
        return True
    try:
        import psycopg
    except ImportError:
        logger.debug("psycopg not installed; skipping fleet lock")
        return True

    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO locks (resource, holder, expires_at) "
                    "VALUES (%s, %s, NOW() + (%s || ' seconds')::interval) "
                    "ON CONFLICT (resource) DO UPDATE "
                    "SET holder = EXCLUDED.holder, expires_at = EXCLUDED.expires_at "
                    "WHERE locks.expires_at < NOW() "
                    "RETURNING holder",
                    (CYCLE_LOCK_RESOURCE, holder, str(CYCLE_LOCK_TTL_SECONDS)),
                )
                row = cur.fetchone()
            conn.commit()
        if row is None:
            return False
        return row[0] == holder
    except Exception as e:
        logger.warning("cycle lock acquire failed (continuing): %s", e)
        return True


def _try_release_cycle_lock(holder: str) -> None:
    dsn = _neon_dsn()
    if not dsn:
        return
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM locks WHERE resource = %s AND holder = %s",
                    (CYCLE_LOCK_RESOURCE, holder),
                )
            conn.commit()
    except Exception as e:
        logger.warning("cycle lock release failed: %s", e)


@contextmanager
def _cycle_lock(holder: str) -> Iterator[bool]:
    acquired = _try_acquire_cycle_lock(holder)
    try:
        yield acquired
    finally:
        if acquired:
            _try_release_cycle_lock(holder)


def _cap_spawn_sessions(moves, limit):
    """Drop spawn_session moves beyond `limit`; preserve order and other kinds."""
    out = []
    spawned = 0
    for m in moves:
        if m.kind == "spawn_session":
            if spawned >= limit:
                continue
            spawned += 1
        out.append(m)
    return out


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

    holder = uuid.uuid4().hex[:12]
    with _cycle_lock(holder) as lock_ok:
        if not lock_ok:
            _log("director.cycle_skipped", reason="another cycle holds director:cycle")
            return 0

        async with track_run(
            "perceive_ideate_act_cycle", intent="auto-nudge"
        ) as fleet_run:
            # Make the fleet run id available to act() helpers so I/O
            # capture (artifacts + decisions) attaches to the right row.
            # When MCP is unreachable run_id is None and capture no-ops.
            fleet_run_id_var.set(fleet_run.run_id)
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

            raw_moves = ideate(snapshot)
            moves = policy.filter_moves(raw_moves, snapshot)
            dropped = len(raw_moves) - len(moves)
            _log(
                "director.ideated",
                count=len(moves),
                dropped=dropped,
                moves=[asdict(m) for m in moves],
            )
            await event(
                "ideated",
                {"moves": len(moves), "dropped": dropped},
                run=fleet_run,
            )
            if dropped:
                await event(
                    "policy.dropped",
                    {"count": dropped},
                    run=fleet_run,
                )

            session_limit = adaptive_session_limit()
            moves = _cap_spawn_sessions(moves, session_limit)
            _log("director.concurrency", limit=session_limit)
            await event(
                "concurrency.limit",
                {"limit": session_limit},
                run=fleet_run,
            )

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
            fleet_run.summary["dropped_by_policy"] = dropped
            fleet_run.summary["session_limit"] = session_limit
            await event(
                "acted",
                {"executed": executed, "total": len(results)},
                run=fleet_run,
            )

            _log("director.done", executed=executed)

    return 0


if __name__ == "__main__":
    sys.exit(run())
