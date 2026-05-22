"""Epic watcher: polls active epics every 60s and grades droid progress.

For each active epic: polls Factory session, calls an LLM grader to judge
progress (making_progress | stuck | failed | done), reprompts the droid via
Factory send_message API if stuck, files sub-issues if blocked, marks done
if criteria met. Auto-closes if grader_passes + CI_green on the linked PR.

Worker D: implement the full polling loop, LLM grader, and auto-close logic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
FACTORY_API_BASE = os.environ.get("FACTORY_API_BASE", "https://api.factory.ai/api/v0")
POLL_INTERVAL_SECONDS = 60
MAX_REPROMPTS = 5


@dataclass
class GraderResult:
    state: str  # making_progress | stuck | failed | done
    reasoning: str
    next_action: str


@dataclass
class EpicWatchState:
    epic_id: int
    session_id: str | None
    status: str
    reprompt_count: int = 0
    last_progress_at: datetime | None = None
    cost_so_far_usd: float = 0.0
    max_cost_usd: float | None = None


async def poll_active_epics(mcp_url: str, mcp_token: str) -> list[dict[str, Any]]:
    """Fetch active epics from the MCP server."""
    raise NotImplementedError("Worker D: implement poll_active_epics")


async def fetch_factory_session(
    session_id: str, factory_api_key: str
) -> dict[str, Any]:
    """Get Factory session status and recent messages."""
    raise NotImplementedError("Worker D: implement fetch_factory_session")


async def grade_progress(epic: dict[str, Any], session: dict[str, Any]) -> GraderResult:
    """Call DeepSeek to grade droid progress against epic success criteria.

    Returns {state, reasoning, next_action} where state is one of:
        making_progress, stuck, failed, done
    """
    raise NotImplementedError("Worker D: implement grade_progress")


async def reprompt_droid(
    session_id: str, message: str, factory_api_key: str
) -> bool:
    """Send a structured nudge to the droid session via Factory send_message API."""
    raise NotImplementedError("Worker D: implement reprompt_droid")


async def verify_criteria(
    epic: dict[str, Any], session: dict[str, Any]
) -> tuple[bool, str]:
    """LLM-based verification of success criteria. Returns (passes, reasoning)."""
    raise NotImplementedError("Worker D: implement verify_criteria")


async def close_epic(
    epic_id: int, mcp_url: str, mcp_token: str, outcome: str
) -> None:
    """Mark epic as closed/abandoned and post closeout comment on GitHub Issue."""
    raise NotImplementedError("Worker D: implement close_epic")


async def watcher_tick(
    mcp_url: str, mcp_token: str, factory_api_key: str, deepseek_api_key: str
) -> dict[str, Any]:
    """Single tick of the watcher loop. Called every 60s.

    Returns summary of actions taken this tick.
    """
    raise NotImplementedError("Worker D: implement watcher_tick")


async def run_watcher_loop() -> None:
    """Main loop: poll every POLL_INTERVAL_SECONDS."""
    raise NotImplementedError("Worker D: implement run_watcher_loop")


if __name__ == "__main__":
    asyncio.run(run_watcher_loop())
