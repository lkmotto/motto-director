"""Always-on worker that claims approved pending_moves and dispatches them.

Architectural fit
=================

  director cron (every 15-30 min)
    → perceive → ideate → enqueue approved rows
  apply_approved cron (in-process drain, single worker, top_n=5)
    → list_by_status('approved') → act() → mark_applied/mark_failed
  worker.py (THIS module, always-on, N workers safe)
    → MCP claim_next_step → act() → mark_applied/mark_failed

The new worker layer lets us run multiple droid-pool consumers without
double-claim risk, because claim_next_step uses
``FOR UPDATE SKIP LOCKED`` server-side (motto-mcp-server PR #63).

Doctrine
--------
- Director nudges. The worker dispatches. The Factory droid executes.
- This loop ONLY runs factory_droid kinds by default — the safer kinds
  (file_issue, merge_pr, etc.) stay in the in-process drain where they
  already work fine. Override via ``DIRECTOR_WORKER_KINDS`` (comma-sep).
- If MCP is unreachable, the worker sleeps and retries — does NOT fall
  back to direct DB access. The whole point is atomic claim semantics.
- Each tick is bounded: claim at most ``DIRECTOR_WORKER_BATCH`` rows
  (default 3) so a backed-up queue can't starve other workers.

Environment
-----------
  MOTTO_MCP_URL              required — MCP server base URL
  MOTTO_MCP_AUTH_TOKEN       required — Bearer token
  NEON_DATABASE_URL          required for mark_applied / mark_failed
  DIRECTOR_WORKER_POLL_SECS  optional, default 30
  DIRECTOR_WORKER_BATCH      optional, default 3 (1..10 per MCP contract)
  DIRECTOR_WORKER_KINDS      optional CSV, default "factory_droid"
  DIRECTOR_WORKER_RUNNER_ID  optional, default "nf-director-worker"
  DIRECTOR_DRY_RUN           respected by act() — worker exits-after-N
                             ticks when set so cron can run in dry mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime

from director import fleet, queue
from director.act import act, fleet_run_id_var
from director.observability import event, init_observability
from director.perceive import Snapshot

logger = logging.getLogger(__name__)


def _log(event_name: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event_name,
        **fields,
    }
    print(json.dumps(record, default=str), flush=True)


def _poll_seconds() -> int:
    try:
        return max(5, int(os.environ.get("DIRECTOR_WORKER_POLL_SECS", "30")))
    except ValueError:
        return 30


def _batch_size() -> int:
    try:
        return max(1, min(10, int(os.environ.get("DIRECTOR_WORKER_BATCH", "3"))))
    except ValueError:
        return 3


def _kinds() -> list[str]:
    raw = os.environ.get("DIRECTOR_WORKER_KINDS", "factory_droid")
    return [k.strip() for k in raw.split(",") if k.strip()]


def _runner_id() -> str:
    return os.environ.get("DIRECTOR_WORKER_RUNNER_ID", "nf-director-worker")


async def _claim_via_mcp(runner_id: str, kinds: list[str], limit: int) -> list[dict]:
    """Call the MCP claim_next_step tool. Returns [] on any error.

    Errors are warning-logged, never raised — the polling loop must
    survive transient MCP outages so droids can resume as soon as the
    server is back. The next tick re-attempts.
    """
    if not fleet._mcp_enabled():  # noqa: SLF001
        return []
    try:
        async with fleet._mcp_client() as c:  # noqa: SLF001
            resp = await c.call_tool(
                "claim_next_step",
                {"runner_id": runner_id, "kinds": kinds, "limit": limit},
            )
            data = getattr(resp, "data", None) or {}
            if not data.get("ok"):
                _log("worker.claim_error", error=str(data.get("error", "unknown"))[:200])
                return []
            return list(data.get("claimed") or [])
    except Exception as exc:  # noqa: BLE001
        _log("worker.claim_failed", error=str(exc)[:200])
        return []


async def _release_via_mcp(move_id: int, runner_id: str, reason: str) -> None:
    """Best-effort release. Used when act() returns dry_run/skipped so
    the row goes back to 'approved' and another worker (or the next
    tick) can re-try. Never raises.
    """
    if not fleet._mcp_enabled():  # noqa: SLF001
        return
    try:
        async with fleet._mcp_client() as c:  # noqa: SLF001
            await c.call_tool(
                "release_claimed_step",
                {"move_id": int(move_id), "runner_id": runner_id, "reason": reason[:200]},
            )
    except Exception as exc:  # noqa: BLE001
        _log("worker.release_failed", move_id=move_id, error=str(exc)[:200])


def _empty_snapshot() -> Snapshot:
    """The worker doesn't perceive — claim_next_step rows have already
    passed policy at proposal time. act() only needs a Snapshot for
    _find_issue/_find_pr lookups, and an empty one is a safe degraded
    mode (target = None → policy gates fall back to defaults).
    """
    return Snapshot(captured_at=datetime.now(UTC).isoformat(), repos=[])


async def _tick(runner_id: str, kinds: list[str], batch: int) -> dict[str, int]:
    """One claim-and-dispatch cycle. Returns counters for the loop log."""
    claimed = await _claim_via_mcp(runner_id, kinds, batch)
    if not claimed:
        _log("worker.idle", batch=batch, kinds=kinds)
        return {"claimed": 0, "applied": 0, "failed": 0, "released": 0}

    fleet_run_id_var.set(f"worker-{runner_id}")
    moves = []
    move_to_row_id: dict[int, int] = {}
    for row in claimed:
        try:
            m = queue.row_to_move(row)
        except Exception as exc:  # noqa: BLE001
            row_id = int(row.get("id") or 0)
            queue.mark_failed(row_id, detail=f"row_to_move: {exc}"[:500])
            _log("worker.row_to_move_failed", row_id=row_id, error=str(exc)[:200])
            continue
        moves.append(m)
        move_to_row_id[id(m)] = int(row["id"])

    if not moves:
        return {"claimed": len(claimed), "applied": 0, "failed": 0, "released": 0}

    results = act(moves, _empty_snapshot(), top_n=len(moves))
    applied = failed = released = 0
    for r in results:
        row_id = move_to_row_id.get(id(r.move))
        if row_id is None:
            continue
        if r.status == "executed":
            queue.mark_applied(row_id, detail=r.detail or "")
            applied += 1
        elif r.status in ("dry_run", "skipped"):
            # Release back to approved so this row doesn't sit in
            # 'claimed' forever while the operator un-sets dry_run or
            # a transient skip-condition clears.
            await _release_via_mcp(row_id, runner_id, r.detail or r.status)
            released += 1
        else:
            queue.mark_failed(row_id, detail=f"{r.status}: {r.detail}"[:500])
            failed += 1

    _log(
        "worker.tick",
        claimed=len(claimed),
        applied=applied,
        failed=failed,
        released=released,
        moves=[asdict(m) for m in moves],
    )
    try:
        await event(
            "worker.tick",
            {
                "runner_id": runner_id,
                "claimed": len(claimed),
                "applied": applied,
                "failed": failed,
                "released": released,
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return {"claimed": len(claimed), "applied": applied, "failed": failed, "released": released}


async def _run_async(max_ticks: int | None = None) -> int:
    """Main polling loop. ``max_ticks`` bounds the loop for tests/dry-run.

    Returns the number of ticks actually executed.
    """
    init_observability("motto-director-worker")
    runner_id = _runner_id()
    poll = _poll_seconds()
    batch = _batch_size()
    kinds = _kinds()

    _log(
        "worker.start",
        runner_id=runner_id,
        poll_seconds=poll,
        batch=batch,
        kinds=kinds,
        has_motto_mcp_url=bool(os.environ.get("MOTTO_MCP_URL")),
        has_motto_mcp_auth_token=bool(os.environ.get("MOTTO_MCP_AUTH_TOKEN")),
        has_neon_database_url=bool(os.environ.get("NEON_DATABASE_URL")),
    )

    ticks = 0
    while True:
        try:
            await _tick(runner_id, kinds, batch)
        except Exception as exc:  # noqa: BLE001
            # Catastrophic tick failure — log and keep looping. The
            # alternative (exit) would let the Northflank service
            # restart-loop on a single bad row, which is worse.
            _log("worker.tick_failed", error=str(exc)[:200])
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            return ticks
        await asyncio.sleep(poll)


def run() -> int:
    """Console-script entry point. Runs forever until SIGTERM."""
    logging.basicConfig(
        level=os.environ.get("DIRECTOR_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    try:
        asyncio.run(_run_async())
        return 0
    except KeyboardInterrupt:
        _log("worker.shutdown", reason="keyboard_interrupt")
        return 0


if __name__ == "__main__":
    sys.exit(run())
