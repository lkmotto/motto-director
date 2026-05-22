"""Epics: multi-cycle projects the director drives KPI-by-KPI.

A *move* is a single action (file an issue, rebase a PR, write code).
An *epic* is a 3-8 step plan tied to a KPI gap. The planner emits epics;
epic_executor picks the next move from each open epic on each cycle and
queues it as a normal pending_move with `epic_id` set.

Status lifecycle:
    proposed  -> human approves the epic itself (cockpit /epics view)
    active    -> executor pulls one move per cycle from `plan` until done
    paused    -> human pauses; executor skips it
    closed    -> all moves applied OR human marked done
    abandoned -> human killed it (KPI changed, approach wrong, etc.)

Schema lives in migrations/0006_epics.sql.

Plan shape (stored as JSONB in `plan`):
    {
      "steps": [
        {"order": 1, "title": "...", "kind": "spawn_session"|"file_issue"|...,
         "repo": "...", "rationale": "...", "depends_on": []},
        ...
      ],
      "kpi_ref": "AMC panel registrations",
      "estimated_cycles": 5,
      "success_criteria": "..."
    }

Status of each step is tracked separately by joining to pending_moves on
epic_id + step_order; we don't mutate the plan JSON itself so the
original plan is preserved as an audit log of what the planner intended.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def _dsn() -> str | None:
    return os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")


def is_configured() -> bool:
    return bool(_dsn())


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class EpicStep:
    order: int
    title: str
    kind: str  # spawn_session | file_issue | compound_pr | ...
    repo: str
    rationale: str = ""
    depends_on: list[int] = field(default_factory=list)


@dataclass
class Epic:
    """In-memory representation of an epic, including its plan steps."""

    title: str
    kpi_ref: str
    rationale: str
    estimated_cycles: int
    success_criteria: str
    steps: list[EpicStep]
    id: int | None = None
    status: str = "proposed"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    run_id: str | None = None

    def plan_payload(self) -> dict[str, Any]:
        return {
            "kpi_ref": self.kpi_ref,
            "estimated_cycles": self.estimated_cycles,
            "success_criteria": self.success_criteria,
            "steps": [asdict(s) for s in self.steps],
        }


# ---------------------------------------------------------------------------
# Insert / update
# ---------------------------------------------------------------------------


def insert_epics(epics: list[Epic], *, run_id: str) -> dict[str, int]:
    """Insert proposed epics. Returns counts dict."""
    counts = {"inserted": 0, "skipped": 0, "errors": 0}
    if not is_configured():
        logger.warning("epics.insert: no DSN; dropping %d epics", len(epics))
        counts["errors"] = len(epics)
        return counts
    if not epics:
        return counts
    try:
        import psycopg
        from psycopg import errors as pgerrors
    except ImportError:
        logger.warning("epics.insert: psycopg not installed")
        counts["errors"] = len(epics)
        return counts

    dsn = _dsn() or ""
    sql = (
        "INSERT INTO epics "
        "(run_id, title, kpi_ref, rationale, estimated_cycles, "
        "success_criteria, plan, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 'proposed')"
    )
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            for e in epics:
                try:
                    cur.execute(
                        sql,
                        (
                            run_id,
                            e.title,
                            e.kpi_ref,
                            e.rationale,
                            int(e.estimated_cycles),
                            e.success_criteria,
                            json.dumps(e.plan_payload(), default=str),
                        ),
                    )
                    conn.commit()
                    counts["inserted"] += 1
                except pgerrors.UniqueViolation:
                    conn.rollback()
                    counts["skipped"] += 1
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    logger.warning("epics.insert error: %s", exc)
                    counts["errors"] += 1
    return counts


def list_active_epics() -> list[Epic]:
    """Return epics in status='active' (approved + executing)."""
    if not is_configured():
        return []
    try:
        import psycopg
    except ImportError:
        return []
    dsn = _dsn() or ""
    out: list[Epic] = []
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, run_id, title, kpi_ref, rationale, "
                "estimated_cycles, success_criteria, plan, status, "
                "created_at, updated_at "
                "FROM epics WHERE status='active' ORDER BY created_at"
            )
            for row in cur.fetchall():
                (
                    id_,
                    run_id,
                    title,
                    kpi_ref,
                    rationale,
                    est_cycles,
                    criteria,
                    plan,
                    status,
                    created,
                    updated,
                ) = row
                plan_dict = plan if isinstance(plan, dict) else json.loads(plan)
                steps = [
                    EpicStep(
                        order=int(s.get("order", 0)),
                        title=str(s.get("title", "")),
                        kind=str(s.get("kind", "")),
                        repo=str(s.get("repo", "")),
                        rationale=str(s.get("rationale", "")),
                        depends_on=list(s.get("depends_on", []) or []),
                    )
                    for s in plan_dict.get("steps", []) or []
                ]
                out.append(
                    Epic(
                        id=int(id_),
                        run_id=run_id,
                        title=title,
                        kpi_ref=kpi_ref,
                        rationale=rationale,
                        estimated_cycles=int(est_cycles or 0),
                        success_criteria=criteria,
                        steps=steps,
                        status=status,
                        created_at=created,
                        updated_at=updated,
                    )
                )
    return out


def count_open_kpis() -> set[str]:
    """Set of kpi_refs with active or proposed (not closed/abandoned) epics.

    Used by the planner to avoid proposing two epics for the same KPI.
    """
    if not is_configured():
        return set()
    try:
        import psycopg
    except ImportError:
        return set()
    dsn = _dsn() or ""
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT kpi_ref FROM epics WHERE status IN ('proposed','active','paused')"
            )
            return {r[0] for r in cur.fetchall()}


def applied_step_orders(epic_id: int) -> set[int]:
    """Returns the set of step.order values for which a pending_move with
    matching epic_id+step_order has status in ('applied','approved').

    Used by the executor: 'pick the next step whose order is not in this set'.
    """
    if not is_configured():
        return set()
    try:
        import psycopg
    except ImportError:
        return set()
    dsn = _dsn() or ""
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT (move_payload->>'step_order')::int "
                "FROM pending_moves "
                "WHERE (move_payload->>'epic_id')::int = %s "
                "  AND status IN ('applied','approved','pending') "
                "  AND move_payload ? 'step_order'",
                (epic_id,),
            )
            return {r[0] for r in cur.fetchall() if r[0] is not None}
