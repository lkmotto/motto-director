"""Pending-moves queue: human-in-the-loop approval gate for director moves.

When ``DIRECTOR_APPROVAL_MODE=manual`` is set (or the per-tick caller passes
``manual_mode=True``), :func:`enqueue_moves` writes rows to ``pending_moves``
instead of executing through :mod:`director.act`.  The cockpit ``/director``
UI and the Telegram ``/director`` command both read/write the same table.

Status lifecycle:
    pending -> approved -> applied
    pending -> rejected (terminal)
    pending -> expired  (terminal, garbage-collected by GC job)
    approved -> failed  (execution error, terminal)

This module is a thin sync wrapper over psycopg.  Callers in :mod:`act` are
sync; callers in the cockpit (FastAPI/FastMCP) are async and should use
:func:`asyncio.to_thread` if they need to call these helpers.

Design notes:
- Schema lives in ``migrations/0005_pending_moves.sql``.
- Dedup is enforced at the DB layer via a partial unique index on
  ``(repo, kind, lower(title)) WHERE status='pending'``.  We catch the
  unique-violation and skip — multiple ticks proposing the same move while
  it sits awaiting review must NOT create duplicates.
- ``move_payload`` stores the full NextMove asdict so the apply job can
  reconstruct it without consulting the snapshot from the proposing tick.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from director.ideate import NextMove

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------

def manual_mode_enabled() -> bool:
    """True when DIRECTOR_APPROVAL_MODE=manual.

    Any other value (including empty/unset/'auto') means auto-execute.
    """
    val = os.environ.get("DIRECTOR_APPROVAL_MODE", "auto").strip().lower()
    return val == "manual"


def _dsn() -> str | None:
    return (
        os.environ.get("NEON_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )


def is_configured() -> bool:
    return bool(_dsn())


# ---------------------------------------------------------------------------
# Enqueue (called from act.py when manual_mode_enabled())
# ---------------------------------------------------------------------------

def enqueue_moves(
    moves: list[NextMove],
    *,
    run_id: str,
) -> dict[str, int]:
    """Write each move as a pending row.

    Returns a counts dict: {"queued": N, "deduped": M, "errors": K}.

    Dedup: if a row with status='pending' already exists for the same
    (repo, kind, lower(title)), the unique index raises and we count it as
    deduped (not an error).
    """
    counts = {"queued": 0, "deduped": 0, "errors": 0}
    if not is_configured():
        logger.warning("queue.enqueue_moves: no DSN configured; dropping %d moves",
                       len(moves))
        counts["errors"] = len(moves)
        return counts
    try:
        import psycopg
        from psycopg import errors as pgerrors
    except ImportError:
        logger.warning("queue.enqueue_moves: psycopg not installed; dropping %d moves",
                       len(moves))
        counts["errors"] = len(moves)
        return counts

    dsn = _dsn() or ""
    sql = (
        "INSERT INTO pending_moves "
        "(run_id, repo, kind, title, rationale, intent, priority, move_payload) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)"
    )
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            for m in moves:
                payload = json.dumps(asdict(m), default=str)
                try:
                    cur.execute(
                        sql,
                        (
                            run_id,
                            m.repo,
                            m.kind,
                            m.title,
                            m.rationale or "",
                            m.intent or "",
                            int(m.priority or 0),
                            payload,
                        ),
                    )
                    conn.commit()
                    counts["queued"] += 1
                except pgerrors.UniqueViolation:
                    conn.rollback()
                    counts["deduped"] += 1
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    counts["errors"] += 1
                    logger.warning(
                        "queue.enqueue_moves row failed (%s/%s): %s",
                        m.repo, m.kind, exc,
                    )
    return counts


# ---------------------------------------------------------------------------
# Read helpers (called from cockpit + telegram bot)
# ---------------------------------------------------------------------------

def list_pending(limit: int = 50) -> list[dict[str, Any]]:
    """Return up to ``limit`` pending rows newest-first."""
    return _list_by_status("pending", limit)


def list_by_status(status: str, limit: int = 50) -> list[dict[str, Any]]:
    return _list_by_status(status, limit)


def _list_by_status(status: str, limit: int) -> list[dict[str, Any]]:
    if not is_configured():
        return []
    try:
        import psycopg
    except ImportError:
        return []
    dsn = _dsn() or ""
    sql = (
        "SELECT id, run_id, created_at, updated_at, repo, kind, title, "
        "rationale, intent, priority, move_payload, status, approved_by, "
        "approved_at, applied_at, apply_detail "
        "FROM pending_moves WHERE status = %s "
        "ORDER BY priority DESC, created_at DESC LIMIT %s"
    )
    out: list[dict[str, Any]] = []
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (status, int(limit)))
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                rec = dict(zip(cols, row, strict=False))
                # Normalize datetimes to ISO strings for easy JSON.
                for k in ("created_at", "updated_at", "approved_at", "applied_at"):
                    v = rec.get(k)
                    if isinstance(v, datetime):
                        rec[k] = v.isoformat()
                # move_payload comes back as dict already (jsonb).
                out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Approve / reject (called from cockpit + telegram)
# ---------------------------------------------------------------------------

def approve(move_id: int, *, approved_by: str) -> bool:
    """Mark a pending row approved. Returns True if a row was updated."""
    return _transition(
        move_id,
        from_status="pending",
        to_status="approved",
        approved_by=approved_by,
    )


def reject(move_id: int, *, approved_by: str) -> bool:
    """Mark a pending row rejected (terminal). Returns True if updated."""
    return _transition(
        move_id,
        from_status="pending",
        to_status="rejected",
        approved_by=approved_by,
    )


def mark_applied(move_id: int, *, detail: str = "") -> bool:
    return _transition(
        move_id,
        from_status="approved",
        to_status="applied",
        applied_at=datetime.now(UTC),
        apply_detail=detail,
    )


def mark_failed(move_id: int, *, detail: str) -> bool:
    return _transition(
        move_id,
        from_status="approved",
        to_status="failed",
        applied_at=datetime.now(UTC),
        apply_detail=detail,
    )


def _transition(
    move_id: int,
    *,
    from_status: str,
    to_status: str,
    approved_by: str | None = None,
    applied_at: datetime | None = None,
    apply_detail: str | None = None,
) -> bool:
    if not is_configured():
        return False
    try:
        import psycopg
    except ImportError:
        return False

    dsn = _dsn() or ""
    sets = ["status = %s", "updated_at = NOW()"]
    args: list[Any] = [to_status]
    if approved_by is not None:
        sets += ["approved_by = %s", "approved_at = NOW()"]
        args.append(approved_by)
    if applied_at is not None:
        sets.append("applied_at = %s")
        args.append(applied_at)
    if apply_detail is not None:
        sets.append("apply_detail = %s")
        args.append(apply_detail)
    args.extend([int(move_id), from_status])
    sql = (
        f"UPDATE pending_moves SET {', '.join(sets)} "
        "WHERE id = %s AND status = %s"
    )
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            updated = cur.rowcount
            conn.commit()
    return bool(updated)


# ---------------------------------------------------------------------------
# Bulk approve (cockpit "approve all" button)
# ---------------------------------------------------------------------------

def bulk_approve(move_ids: list[int], *, approved_by: str) -> int:
    """Approve many pending rows in one transaction. Returns count updated."""
    if not move_ids or not is_configured():
        return 0
    try:
        import psycopg
    except ImportError:
        return 0
    dsn = _dsn() or ""
    sql = (
        "UPDATE pending_moves SET status='approved', approved_by=%s, "
        "approved_at=NOW(), updated_at=NOW() "
        "WHERE id = ANY(%s) AND status='pending'"
    )
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (approved_by, list(move_ids)))
            updated = cur.rowcount
            conn.commit()
    return int(updated or 0)


# ---------------------------------------------------------------------------
# GC (called by a periodic job, or every Nth tick)
# ---------------------------------------------------------------------------

def expire_stale(older_than_hours: int = 48) -> int:
    """Mark pending rows older than the threshold as 'expired'."""
    if not is_configured():
        return 0
    try:
        import psycopg
    except ImportError:
        return 0
    dsn = _dsn() or ""
    sql = (
        "UPDATE pending_moves SET status='expired', updated_at=NOW() "
        "WHERE status='pending' "
        "  AND created_at < NOW() - (%s || ' hours')::interval"
    )
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (str(int(older_than_hours)),))
            n = cur.rowcount
            conn.commit()
    return int(n or 0)


# ---------------------------------------------------------------------------
# Reconstruction for the apply job
# ---------------------------------------------------------------------------

def row_to_move(row: dict[str, Any]) -> NextMove:
    """Reconstruct a NextMove from a pending_moves row."""
    payload = row.get("move_payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    return NextMove(
        repo=payload.get("repo", row["repo"]),
        kind=payload.get("kind", row["kind"]),
        title=payload.get("title", row["title"]),
        rationale=payload.get("rationale", row.get("rationale", "")),
        prompt_for_claude_code=payload.get("prompt_for_claude_code", ""),
        priority=int(payload.get("priority", row.get("priority", 0))),
        intent=payload.get("intent", row.get("intent", "")),
        code_changes=payload.get("code_changes", []) or [],
        epic_id=payload.get("epic_id"),
        step_order=payload.get("step_order"),
        target_move_id=payload.get("target_move_id"),
    )
