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
    if not queue.is_configured():
        _log("apply.skipped", reason="no_dsn")
        return 0

    rows = queue.list_by_status("approved", limit=_max_per_run())
    if not rows:
        _log("apply.empty")
        return 0

    snapshot = await perceive()
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
    for r in results:
        row_id = id_for_move.get(id(r.move))
        if row_id is None:
            continue
        if r.status == "executed":
            queue.mark_applied(row_id, detail=r.detail or "")
            applied += 1
        elif r.status in ("dry_run", "skipped"):
            # Don't terminate the row — leave it approved so the next
            # apply run picks it up when the dry-run flag is off.
            pass
        else:
            queue.mark_failed(row_id, detail=f"{r.status}: {r.detail}")
            failed += 1

    _log("apply.done", applied=applied, failed=failed,
         total=len(results), moves=[asdict(m) for m in moves])
    return 0


def run() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    sys.exit(run())
