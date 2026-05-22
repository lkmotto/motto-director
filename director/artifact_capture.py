"""Thin helper fleet agents import to record their outputs.

Usage from any agent (sdr-agent, appraisal-pipeline, video-agent, etc.):

    from director.artifact_capture import record_artifact_content

    artifact_id = await record_artifact_content(
        agent_name="motto-sdr-agent",
        kind="cold_email",
        body=draft_text,
        intent="Outreach to bank lender about appraisal turnaround time",
        repo="lkmotto/motto-sdr-agent",
        send_blocking=True,  # don't dispatch until critic has reviewed
    )

Why this lives in motto-director: keeps the MCP tool shape in one place
so callers don't each maintain their own fastmcp client. Other repos
either pip-install this package or vendor this single file.

The motto-director repo will publish this as `motto_director` on the
internal index; in the meantime, agents can `pip install
git+https://github.com/lkmotto/motto-director.git` and import directly.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _mcp_configured() -> bool:
    return bool(os.environ.get("MOTTO_MCP_URL")) and bool(os.environ.get("MOTTO_MCP_AUTH_TOKEN"))


async def record_artifact_content(
    *,
    agent_name: str,
    kind: str,
    body: str,
    name: str | None = None,
    run_id: str | None = None,
    intent: str | None = None,
    repo: str | None = None,
    meta: dict[str, Any] | None = None,
    send_blocking: bool = False,
) -> int | None:
    """Store a real output artifact in fleet.artifacts.content JSONB.

    Returns the artifact id on success, None on any failure (MCP not
    configured, network error, server error). Agents should treat this
    as best-effort observability — failure to record must NOT block
    the agent's actual job.

    Parameters mirror the MCP tool exactly; see motto-mcp-server
    `record_artifact_content` docstring for semantics.
    """
    if not _mcp_configured():
        logger.debug("record_artifact_content: MCP not configured, skipping")
        return None
    try:
        from fastmcp import Client
        from fastmcp.client.auth import BearerAuth

        url = os.environ["MOTTO_MCP_URL"]
        token = os.environ["MOTTO_MCP_AUTH_TOKEN"]
        async with Client(url, auth=BearerAuth(token)) as c:
            resp = await c.call_tool(
                "record_artifact_content",
                {
                    "agent_name": agent_name,
                    "kind": kind,
                    "body": body,
                    "name": name,
                    "run_id": run_id,
                    "intent": intent,
                    "repo": repo,
                    "meta": meta or {},
                    "send_blocking": bool(send_blocking),
                },
            )
            data = getattr(resp, "data", None)
            if isinstance(data, dict):
                aid = data.get("artifact_id")
                if isinstance(aid, int):
                    return aid
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_artifact_content failed: %s", exc)
        return None


async def get_review_status(artifact_id: int) -> str | None:
    """Read the current review_status of an artifact.

    Send-blocking agents should call this before dispatching:

        status = await get_review_status(aid)
        if status == "passed":
            send()
        elif status in ("flagged", "blocked"):
            # do not send; route to human review queue
            ...
        else:
            # 'pending' or None — wait for next tick
            ...

    Returns None if MCP is not configured or the artifact is missing.
    """
    if not _mcp_configured():
        return None
    try:
        from fastmcp import Client
        from fastmcp.client.auth import BearerAuth

        url = os.environ["MOTTO_MCP_URL"]
        token = os.environ["MOTTO_MCP_AUTH_TOKEN"]
        # No dedicated MCP read-by-id tool yet; reuse pending-review
        # query and filter. Cheap because it's bounded.
        async with Client(url, auth=BearerAuth(token)) as c:
            resp = await c.call_tool(
                "artifacts_pending_review",
                {"since_hours": 168, "limit": 100},
            )
            data = getattr(resp, "data", None)
            if isinstance(data, list):
                for row in data:
                    if int(row.get("id", -1)) == int(artifact_id):
                        content = row.get("content") or {}
                        status = content.get("review_status")
                        return str(status) if status else "pending"
            # If not in pending list, it's been reviewed — but we don't
            # have a way to read finalized state via current tools. The
            # caller should treat None as "not pending; check elsewhere".
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_review_status failed: %s", exc)
        return None
