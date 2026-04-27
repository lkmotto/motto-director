"""Entrypoint: perceive → ideate → act loop. Designed for Northflank cron."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime

from director.act import act
from director.ideate import ideate
from director.perceive import perceive


def _log(event: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), flush=True)


def run() -> int:
    _log("director.start")

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
    )

    moves = ideate(snapshot)
    _log("director.ideated", count=len(moves), moves=[asdict(m) for m in moves])

    results = act(moves, snapshot)
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

    _log("director.done", executed=sum(1 for r in results if r.status == "executed"))
    return 0


if __name__ == "__main__":
    sys.exit(run())
