"""Postgres signals: task/decision health from the Neon control-plane.

Same shape contract as langfuse_signals.collect() — a stable dict the
synthesizer can consume even when the data source isn't configured.

Reads via psycopg (already a transitive dep through main.py's fleet lock
helpers) when NEON_DATABASE_URL or DATABASE_URL is set; otherwise no-ops
and returns the empty signal blob.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _dsn() -> str | None:
    return os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")


def is_configured() -> bool:
    return bool(_dsn())


async def collect(since_days: int = 7) -> dict[str, Any]:
    """Return task-success / decisions-overturned / repeated-failure signals.

    Empty when Neon isn't reachable or psycopg isn't installed — caller
    treats empty as a degraded signal, not an error.
    """
    if not is_configured():
        return _empty()
    try:
        import psycopg  # noqa: F401
    except ImportError:
        logger.info("psycopg not installed; postgres_signals returning empty")
        return _empty()
    try:
        return _collect_sync(_dsn() or "", since_days)
    except Exception as e:  # noqa: BLE001
        logger.warning("postgres_signals.collect failed: %s", e)
        return _empty()


def _empty() -> dict[str, Any]:
    return {
        "configured": False,
        "task_success_rate": 0.0,
        "avg_task_duration_seconds": 0.0,
        "decisions_overturned_count": 0,
        "repeated_failures": [],
        "task_count": 0,
    }


def _collect_sync(dsn: str, since_days: int) -> dict[str, Any]:
    """Run the SQL synchronously inside the asyncio loop's executor would be
    cleaner, but psycopg connections are short-lived here and the cron is
    not latency-sensitive. Keep it simple: blocking calls inside a synchronous
    helper invoked from an async wrapper."""
    import psycopg

    out = _empty()
    out["configured"] = True

    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            # Task success rate + avg duration. The control-plane schema
            # uses runs.status; we treat 'success' as success and anything
            # else as failure for the rate computation.
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'success')::float
                        / NULLIF(COUNT(*), 0) AS success_rate,
                    AVG(EXTRACT(EPOCH FROM (finished_at - started_at)))
                        FILTER (WHERE finished_at IS NOT NULL) AS avg_duration,
                    COUNT(*) AS total
                FROM runs
                WHERE started_at >= NOW() - make_interval(days => %s)
                """,
                (since_days,),
            )
            row = cur.fetchone()
            if row is not None:
                rate, duration, total = row
                out["task_success_rate"] = float(rate or 0.0)
                out["avg_task_duration_seconds"] = float(duration or 0.0)
                out["task_count"] = int(total or 0)

            # Decisions overturned by meta — count of decision events whose
            # payload references a prior decision via 'overturns' field.
            # Falls back to 0 when the column/event shape doesn't exist.
            try:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM events
                    WHERE kind = 'decision'
                      AND ts >= NOW() - make_interval(days => %s)
                      AND (payload ? 'overturns'
                           OR payload->>'choice' = 'overturn_prior_decision')
                    """,
                    (since_days,),
                )
                row = cur.fetchone()
                if row is not None:
                    out["decisions_overturned_count"] = int(row[0] or 0)
            except Exception as e:  # noqa: BLE001
                logger.debug("decisions_overturned query skipped: %s", e)

            # Repeated failures: same (kind, payload->>'error_kind') seen
            # >3 times. We use error_kind if present; else fall back to
            # the first 80 chars of the payload's 'detail' field.
            try:
                cur.execute(
                    """
                    SELECT
                        COALESCE(payload->>'error_kind',
                                 LEFT(payload->>'detail', 80),
                                 kind) AS pattern,
                        COUNT(*) AS hits
                    FROM events
                    WHERE level IN ('warn', 'error')
                      AND ts >= NOW() - make_interval(days => %s)
                    GROUP BY pattern
                    HAVING COUNT(*) > 3
                    ORDER BY hits DESC
                    LIMIT 10
                    """,
                    (since_days,),
                )
                out["repeated_failures"] = [
                    {"pattern": r[0] or "?", "count": int(r[1])} for r in cur.fetchall()
                ]
            except Exception as e:  # noqa: BLE001
                logger.debug("repeated_failures query skipped: %s", e)

    return out


async def persist_quality_report(report_dict: dict[str, Any]) -> int | None:
    """Insert one row into quality_reports. Returns row id, or None if
    Postgres isn't configured / migration hasn't been applied."""
    if not is_configured():
        return None
    try:
        import psycopg
    except ImportError:
        return None
    dsn = _dsn() or ""
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                # Accept both `top_problems` (synthesizer.as_dict) and the
                # legacy `top_3_problems` shape. Either is fine.
                top = report_dict.get("top_problems") or report_dict.get("top_3_problems") or []
                cur.execute(
                    """
                    INSERT INTO quality_reports
                        (signals, top_problems, suggested_fixes,
                         confidence_score, pr_urls, issue_urls)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        _json(report_dict.get("signals") or {}),
                        _json(top),
                        _json(report_dict.get("suggested_fixes") or []),
                        float(report_dict.get("confidence_score") or 0.0),
                        _json(report_dict.get("pr_urls") or []),
                        _json(report_dict.get("issue_urls") or []),
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return int(row[0]) if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning("persist_quality_report failed: %s", e)
        return None


def _json(obj: Any) -> str:
    import json as _json_lib

    return _json_lib.dumps(obj, default=str)
