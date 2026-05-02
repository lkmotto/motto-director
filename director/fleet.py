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
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _mcp_client():
    from fastmcp import Client
    url = os.environ["MOTTO_MCP_URL"]
    token = os.environ["MOTTO_MCP_AUTH_TOKEN"]
    async with Client(transport=url, headers={"Authorization": f"Bearer {token}"}) as c:
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
    from_ts = (
        datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    ).isoformat()

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
