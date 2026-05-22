"""Load strategic intent from motto-strategy.md (or env override).

Strategic intent is prepended to every lens prompt so the director ideates
with a sense of what we're trying to *build*, not just what's currently
broken. Edit `motto-strategy.md` on main; takes effect next cycle.

Resolution order:
  1. STRATEGIC_INTENT env var (raw markdown content) — for testing/override
  2. STRATEGIC_INTENT_PATH env var (path to file)
  3. <repo root>/motto-strategy.md (the canonical location)
  4. Empty string (lens prompts run unchanged)

Failures are silent + logged; never crashes the director cycle.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("director.strategy")

_DEFAULT_FILENAME = "motto-strategy.md"
_MAX_BYTES = 16_000  # safety cap; lens prompts are ~2-4 KB each


def _log(event: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


def _repo_root() -> Path:
    # director/strategy.py → director/ → repo root
    return Path(__file__).resolve().parent.parent


def load_strategic_intent() -> str:
    """Return strategic intent markdown, or empty string if unavailable."""
    raw_env = os.environ.get("STRATEGIC_INTENT", "").strip()
    if raw_env:
        if len(raw_env) > _MAX_BYTES:
            raw_env = raw_env[:_MAX_BYTES] + "\n... [truncated]"
        _log("strategy.loaded", source="env", bytes=len(raw_env))
        return raw_env

    candidates: list[Path] = []
    path_env = os.environ.get("STRATEGIC_INTENT_PATH", "").strip()
    if path_env:
        candidates.append(Path(path_env))
    candidates.append(_repo_root() / _DEFAULT_FILENAME)

    for path in candidates:
        try:
            if path.is_file():
                content = path.read_text(encoding="utf-8")
                if len(content) > _MAX_BYTES:
                    content = content[:_MAX_BYTES] + "\n... [truncated]"
                _log(
                    "strategy.loaded",
                    source=str(path),
                    bytes=len(content),
                )
                return content
        except Exception as exc:  # noqa: BLE001
            _log(
                "strategy.load_failed",
                path=str(path),
                error=str(exc)[:200],
            )

    _log("strategy.empty", reason="no source found")
    return ""


def format_for_prompt(intent: str) -> str:
    """Wrap the raw intent in a clearly-marked block for the lens prompt."""
    if not intent:
        return ""
    return (
        "===== STRATEGIC INTENT (read first; every move should advance one of "
        "these) =====\n" + intent.rstrip() + "\n===== END STRATEGIC INTENT =====\n\n"
    )
