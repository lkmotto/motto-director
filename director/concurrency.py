"""Adaptive concurrency cap for spawn_session moves.

Reads the director's recent run history out of the Neon control plane
(`runs` table) and scales the per-cycle session cap up when the recent
success rate is healthy, back down when it isn't. Falls back to a fixed
DIRECTOR_MAX_CONCURRENT_SESSIONS (or 3) when Neon isn't reachable.
"""

from __future__ import annotations

import logging
import os

from director.policy import director_max_concurrent_default

logger = logging.getLogger(__name__)

BASE_LIMIT = 3
MAX_LIMIT = 6
WINDOW_DAYS = 7
SCALE_UP_THRESHOLD = 0.8
SCALE_DOWN_THRESHOLD = 0.6


def _neon_dsn() -> str | None:
    return os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")


def _success_rate_7d(dsn: str) -> tuple[float, int] | None:
    """Return (success_rate, total_runs) from the last 7 days, or None on
    failure / no data."""
    try:
        import psycopg
    except ImportError:
        logger.debug("psycopg not installed; skipping adaptive concurrency lookup")
        return None

    query = (
        "SELECT "
        "  COUNT(*) FILTER (WHERE status = 'success')::float AS ok, "
        "  COUNT(*)::float AS total "
        "FROM runs "
        f"WHERE started_at >= NOW() - INTERVAL '{WINDOW_DAYS} days' "
        "  AND agent_name = 'motto-director'"
    )
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                row = cur.fetchone()
    except Exception as e:
        logger.warning("adaptive_session_limit Neon query failed: %s", e)
        return None

    if not row:
        return None
    ok, total = row[0] or 0.0, row[1] or 0.0
    if total == 0:
        return None
    return ok / total, int(total)


def adaptive_session_limit(neon_dsn: str | None = None) -> int:
    """Compute concurrent-session cap. Scales 3 → 6 when 7d success > 0.8,
    down to 3 when < 0.6, stays put in between. Falls back to env / 3 when
    Neon isn't configured."""
    dsn = neon_dsn if neon_dsn is not None else _neon_dsn()
    if not dsn:
        return director_max_concurrent_default()

    result = _success_rate_7d(dsn)
    if result is None:
        return BASE_LIMIT

    rate, total = result
    if total < 5:
        # Too little data to trust; stay at baseline.
        return BASE_LIMIT
    if rate >= SCALE_UP_THRESHOLD:
        return MAX_LIMIT
    if rate < SCALE_DOWN_THRESHOLD:
        return BASE_LIMIT
    return BASE_LIMIT + 1
