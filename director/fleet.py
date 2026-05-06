"""Read fleet state from motto-mcp-server + recent traces from Langfuse.

Director consumes these to give perceive→ideate→act richer context for
auto-nudge. Read-only — director writes via observability.event() /
observability.signal_intent_via_mcp() instead.

Returns plain dicts/lists. Empty list when the backing service isn't
configured — callers don't need to special-case missing infra.
"""

from __future__ import annotations

import base64
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _mcp_client():
    from fastmcp import Client
    from fastmcp.client.auth import BearerAuth
    url = os.environ["MOTTO_MCP_URL"]
    token = os.environ["MOTTO_MCP_AUTH_TOKEN"]
    async with Client(url, auth=BearerAuth(token)) as c:
        yield c


def _mcp_enabled() -> bool:
    return bool(os.environ.get("MOTTO_MCP_URL")) and bool(
        os.environ.get("MOTTO_MCP_AUTH_TOKEN")
    )


async def fleet_status() -> list[dict[str, Any]]:
    """Snapshot of every agent in the fleet."""
    if not _mcp_enabled():
        return []
    try:
        async with _mcp_client() as c:
            resp = await c.call_tool("get_fleet_status", {})
            return getattr(resp, "data", []) or []
    except Exception as e:
        logger.warning("fleet_status failed: %s", e)
        return []


async def recent_events(
    *,
    since_minutes: int = 60,
    agent_name: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Recent fleet events, newest first."""
    if not _mcp_enabled():
        return []
    try:
        async with _mcp_client() as c:
            resp = await c.call_tool(
                "get_recent_events",
                {
                    "since_minutes": since_minutes,
                    "agent_name": agent_name,
                    "kind": kind,
                    "limit": limit,
                },
            )
            return getattr(resp, "data", []) or []
    except Exception as e:
        logger.warning("recent_events failed: %s", e)
        return []


async def signal_intent(
    target_agent: str,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> str | None:
    """Post a cross-agent nudge. source defaults to 'motto-director'."""
    if not _mcp_enabled():
        return None
    try:
        async with _mcp_client() as c:
            resp = await c.call_tool(
                "signal_intent",
                {
                    "target_agent": target_agent,
                    "kind": kind,
                    "payload": payload or {},
                    "source_agent": "motto-director",
                },
            )
            data = getattr(resp, "data", None)
            return data.get("intent_id") if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("signal_intent failed: %s", e)
        return None


async def list_runs(
    *,
    agent_name: str | None = None,
    status: str | None = None,
    since_minutes: int = 60 * 24,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List recent fleet runs via the new MCP tool. [] when MCP isn't reachable."""
    if not _mcp_enabled():
        return []
    try:
        async with _mcp_client() as c:
            resp = await c.call_tool(
                "list_runs",
                {
                    "agent_name": agent_name,
                    "status": status,
                    "since_minutes": since_minutes,
                    "limit": limit,
                },
            )
            return getattr(resp, "data", []) or []
    except Exception as e:
        logger.warning("list_runs failed: %s", e)
        return []


async def record_artifact(
    *,
    run_id: str | None,
    kind: str,
    ref: str | None,
    meta: dict[str, Any] | None = None,
) -> str | None:
    """Persist an artifact reference + meta against a run.

    No dedicated MCP tool yet — piggy-backs on `record_event` with
    kind='artifact_added' and the artifact fields in payload. The Neon
    `artifacts` table already has a `meta` JSONB column, so a future
    migration can backfill this from events.
    """
    if not _mcp_enabled():
        return None
    try:
        async with _mcp_client() as c:
            resp = await c.call_tool(
                "record_event",
                {
                    "agent_name": "motto-director",
                    "kind": "artifact_added",
                    "payload": {
                        "artifact_kind": kind,
                        "ref": ref,
                        "meta": meta or {},
                    },
                    "run_id": run_id,
                    "level": "info",
                },
            )
            data = getattr(resp, "data", None)
            return str(data.get("event_id")) if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("record_artifact failed: %s", e)
        return None


async def record_decision(
    *,
    run_id: str | None,
    choice: str,
    rationale: str,
    evidence: dict[str, Any] | None = None,
) -> str | None:
    """Persist a director decision (audit trail) via record_event.

    Same piggy-back as record_artifact — the dedicated decisions tool is
    deferred. choice/rationale/evidence land in the event payload so the
    digest + replay tools can fan them back out.
    """
    if not _mcp_enabled():
        return None
    try:
        async with _mcp_client() as c:
            resp = await c.call_tool(
                "record_event",
                {
                    "agent_name": "motto-director",
                    "kind": "decision",
                    "payload": {
                        "choice": choice,
                        "rationale": rationale,
                        "evidence": evidence or {},
                    },
                    "run_id": run_id,
                    "level": "info",
                },
            )
            data = getattr(resp, "data", None)
            return str(data.get("event_id")) if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("record_decision failed: %s", e)
        return None


async def record_planner_event(
    *,
    run_id: str | None,
    parsed: int,
    inserted: int,
    skipped: int,
    errors: int,
    filtered_kpi_dup: int,
    latency_ms: int,
    tokens_in: int,
    tokens_out: int,
    output_preview: str,
    open_kpi_count: int,
) -> str | None:
    """Persist a planner-cycle observability event to fleet.events.

    Lets us answer 'did the planner fire? what did it produce? why are
    epics rows still empty?' from SQL instead of from NF stdout (which
    isn't reachable via the NF API).
    """
    if not _mcp_enabled():
        return None
    # Truncate output preview hard — events table is for debug breadcrumbs,
    # not full LLM dumps.
    preview = (output_preview or "").strip()
    if len(preview) > 800:
        preview = preview[:800] + "\u2026 [truncated]"
    try:
        async with _mcp_client() as c:
            resp = await c.call_tool(
                "record_event",
                {
                    "agent_name": "motto-director",
                    "kind": "planner.cycle",
                    "payload": {
                        "parsed": parsed,
                        "inserted": inserted,
                        "skipped": skipped,
                        "errors": errors,
                        "filtered_kpi_dup": filtered_kpi_dup,
                        "latency_ms": latency_ms,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "output_preview": preview,
                        "open_kpi_count": open_kpi_count,
                    },
                    "run_id": run_id,
                    "level": "info",
                },
            )
            data = getattr(resp, "data", None)
            return str(data.get("event_id")) if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("record_planner_event failed: %s", e)
        return None


async def langfuse_recent_traces(
    *,
    since_minutes: int = 60,
    agent_name: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Recent Langfuse traces — the LLM-aware view of fleet activity.

    Pairs with fleet_status() (what they did) to give ideate() the LLM
    introspection feed: prompts sent, models used, costs, latencies.
    Returns [] when Langfuse isn't configured.
    """
    host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
    sk = os.environ.get("LANGFUSE_SECRET_KEY")
    if not pk or not sk:
        return []

    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    from_ts = (datetime.now(UTC) - timedelta(minutes=since_minutes)).isoformat()

    params: dict[str, Any] = {"fromTimestamp": from_ts, "limit": limit}
    if agent_name:
        params["name"] = agent_name

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{host}/api/public/traces",
                params=params,
                headers={"Authorization": f"Basic {auth}"},
            )
            r.raise_for_status()
            return r.json().get("data", [])
    except Exception as e:
        logger.warning("langfuse_recent_traces failed: %s", e)
        return []
