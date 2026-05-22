"""Sub-issue filing for parent epics.

Takes (parent_epic_id, title, body, kind) where kind is one of:
    capability_request, cross_repo, blocked, decision_needed

If kind == capability_request, also files via MCP request_capability.
Creates GitHub Issue with parent_epic: #N link in body, labels sub-issue + kind:{kind}.

Worker E: implement the full sub-issue filing pipeline.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def file_sub_issue(
    parent_epic_id: int,
    title: str,
    body: str,
    kind: str,
    repo: str | None = None,
    mcp_url: str | None = None,
    mcp_token: str | None = None,
) -> dict[str, Any]:
    """Create a GitHub sub-issue under a parent epic.

    Args:
        parent_epic_id: The epic's database ID.
        title: Sub-issue title.
        body: Sub-issue body (parent link auto-prepended).
        kind: One of capability_request, cross_repo, blocked, decision_needed.
        repo: Override the repo (defaults to parent epic's repo).
        mcp_url/mcp_token: Needed for capability_request kind.

    Returns:
        {sub_issue_url, sub_issue_number, kind, parent_epic_id, ok}
    """
    if kind not in ("capability_request", "cross_repo", "blocked", "decision_needed"):
        raise ValueError(f"Invalid kind: {kind}")
    raise NotImplementedError("Worker E: implement file_sub_issue")


async def _create_github_issue(
    repo_full_name: str,
    title: str,
    body: str,
    labels: list[str],
) -> dict[str, Any]:
    """Create a GitHub Issue via the REST API."""
    raise NotImplementedError("Worker E: implement _create_github_issue")


async def _post_parent_comment(
    parent_epic_id: int,
    sub_issue_url: str,
    mcp_url: str,
    mcp_token: str,
) -> None:
    """Post a comment on the parent epic's GitHub Issue referencing the new sub-issue."""
    raise NotImplementedError("Worker E: implement _post_parent_comment")
