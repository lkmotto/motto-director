"""Load KPI targets from motto-kpis.md (or env override).

KPIs are prepended to the planner lens prompt so it ideates with explicit
targets (current vs goal) rather than vague aspirations. The planner uses
the gap between current and target to prioritize which epic to propose.

Resolution order (mirrors strategy.py):
  1. KPIS env var (raw markdown content) - for testing/override
  2. KPIS_PATH env var (path to file)
  3. <repo root>/motto-kpis.md
  4. Empty string (planner runs without explicit KPIs; will be vague)

Failures are silent + logged; never crash the director cycle.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("director.kpis")

_DEFAULT_FILENAME = "motto-kpis.md"
_MAX_BYTES = 16_000


def _log(event: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_kpis() -> str:
    """Return KPI markdown, or empty string if unavailable."""
    raw_env = os.environ.get("KPIS", "").strip()
    if raw_env:
        if len(raw_env) > _MAX_BYTES:
            raw_env = raw_env[:_MAX_BYTES] + "\n... [truncated]"
        _log("kpis.loaded", source="env", bytes=len(raw_env))
        return raw_env

    candidates: list[Path] = []
    path_env = os.environ.get("KPIS_PATH", "").strip()
    if path_env:
        candidates.append(Path(path_env))
    candidates.append(_repo_root() / _DEFAULT_FILENAME)

    for path in candidates:
        try:
            if path.is_file():
                content = path.read_text(encoding="utf-8")
                if len(content) > _MAX_BYTES:
                    content = content[:_MAX_BYTES] + "\n... [truncated]"
                _log("kpis.loaded", source=str(path), bytes=len(content))
                return content
        except Exception as exc:  # noqa: BLE001
            _log("kpis.load_failed", path=str(path), error=str(exc)[:200])

    _log("kpis.empty", reason="no source found")
    return ""


def format_for_prompt(kpis: str) -> str:
    """Wrap raw KPI markdown for the planner prompt."""
    if not kpis:
        return ""
    return (
        "===== MOTTO KPIs (current vs target — every epic must close one of "
        "these gaps) =====\n"
        + kpis.rstrip()
        + "\n===== END KPIs =====\n\n"
    )
