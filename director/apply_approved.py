"""Drain approved pending_moves rows by executing them through act().

Designed to run on its own cron (or on-demand from cockpit) when
DIRECTOR_APPROVAL_MODE=manual is the steady state.  Each row is executed
exactly once: row -> NextMove -> act() -> mark_applied / mark_failed.

Snapshot for act():
    act() needs a Snapshot mainly for `_find_pr` and `_find_issue` lookups
    (preventing duplicate file_issue, finding the PR for merge_pr).  We
    build a light snapshot here by re-running perceive() rather than
    persisting the proposing tick's snapshot — same approach as legacy auto
    mode, since approval-time state matters more than proposal-time state
    for the gates inside act().
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime

from director import queue
from director.act import act
from director.observability import event
from director.perceive import perceive


def _log(event_name: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event_name,
        **fields,
    }
    print(json.dumps(record, default=str), flush=True)


def _max_per_run() -> int:
    try:
        return int(os.environ.get("DIRECTOR_APPLY_MAX", "5"))
    except ValueError:
        return 5


async def _async_main() -> int:
    # Emit entry beacon so we can prove the drain ran in fleet.events
    # (stdout-only logs are invisible from cockpit/Telegram).
    try:
        await event("apply.entered", {"max": _max_per_run()})
    except Exception:  # noqa: BLE001
        pass

    if not queue.is_configured():
        _log("apply.skipped", reason="no_dsn")
        try:
            await event("apply.skipped", {"reason": "no_dsn"})
        except Exception:  # noqa: BLE001
            pass
        return 0

    rows = queue.list_by_status("approved", limit=_max_per_run())
    if not rows:
        _log("apply.empty")
        try:
            await event("apply.empty", {})
        except Exception:  # noqa: BLE001
            pass
        return 0

    # Perceive can be flaky (rate-limits, MCP hiccups). Don't let it
    # nuke the entire drain — degrade to an empty snapshot so noop /
    # file_issue / merge_pr rows can still terminate.
    try:
        snapshot = await perceive()
    except Exception as exc:  # noqa: BLE001
        _log("apply.perceive_failed", error=str(exc)[:200])
        try:
            await event("apply.perceive_failed", {"error": str(exc)[:200]}, level="warn")
        except Exception:  # noqa: BLE001
            pass
        from director.perceive import Snapshot  # local import to avoid cycle

        snapshot = Snapshot(captured_at=datetime.now(UTC).isoformat(), repos=[])
    moves = []
    id_for_move: dict[int, int] = {}
    for row in rows:
        try:
            m = queue.row_to_move(row)
        except Exception as exc:  # noqa: BLE001
            queue.mark_failed(int(row["id"]), detail=f"row_to_move: {exc}")
            continue
        moves.append(m)
        id_for_move[id(m)] = int(row["id"])

    if not moves:
        return 0

    results = act(moves, snapshot, top_n=len(moves))
    applied = 0
    failed = 0
    skipped = 0
    for r in results:
        row_id = id_for_move.get(id(r.move))
        if row_id is None:
            continue
        if r.status == "executed":
            queue.mark_applied(row_id, detail=r.detail or "")
            applied += 1
        elif r.status == "skipped" and r.move.kind == "noop":
            # noop has nothing to execute by definition. Terminate the
            # row so it doesn't sit in 'approved' forever blocking the
            # autonomy demo. The detail field preserves the trace.
            queue.mark_applied(row_id, detail=r.detail or "noop")
            applied += 1
        elif r.status in ("dry_run", "skipped"):
            # Real dry-run gating — leave the row approved so the next
            # apply run picks it up when the dry-run flag is off.
            skipped += 1
        else:
            queue.mark_failed(row_id, detail=f"{r.status}: {r.detail}")
            failed += 1

    _log(
        "apply.done",
        applied=applied,
        failed=failed,
        skipped=skipped,
        total=len(results),
        moves=[asdict(m) for m in moves],
    )
    try:
        await event(
            "apply.done",
            {
                "applied": applied,
                "failed": failed,
                "skipped": skipped,
                "total": len(results),
                "move_ids": list(id_for_move.values()),
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return applied


def run() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    sys.exit(run())
