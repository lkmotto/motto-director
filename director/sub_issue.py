"""Sub-issue filing for parent epics.

Takes (parent_epic_id, title, body, kind) where kind is one of:
    capability_request, blocked, decision_needed, observation, cross_repo

Creates a GitHub Issue with a "Parent epic" link in the body and labels
``sub-issue`` + ``kind:{kind}``. Per-kind side effects:

    * ``capability_request`` — also files via MCP ``request_capability``.
    * ``blocked``            — also nudges Luke via MCP
                               ``signal_intent(target_agent='luke',
                               kind='epic_blocked')``.
    * ``decision_needed``    — also enqueues a row in the cockpit
                               ``pending_moves`` queue so a human can
                               approve/reject the call-out.
    * ``observation``        — no extra side effect (record_event still
                               fires).
    * ``cross_repo``         — kept for backwards compatibility with the
                               original Day-0 stub; no extra side effect.

Every kind records an ``epic.sub_issue.filed`` fleet event via MCP, so the
director's perceive lens can see escalations.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

VALID_KINDS = (
    "capability_request",
    "blocked",
    "decision_needed",
    "observation",
    "cross_repo",
)

_SUB_ISSUE_LABEL = "sub-issue"


def _resolve_mcp(mcp_url: str | None, mcp_token: str | None) -> tuple[str | None, str | None]:
    url = mcp_url or os.environ.get("MOTTO_MCP_URL")
    token = mcp_token or os.environ.get("MOTTO_MCP_AUTH_TOKEN")
    if url:
        url = url.rstrip("/")
        if not url.endswith("/mcp"):
            url = f"{url}/mcp"
    return url, token


@asynccontextmanager
async def _mcp_client(url: str, token: str):
    from fastmcp import Client
    from fastmcp.client.auth import BearerAuth

    async with Client(url, auth=BearerAuth(token)) as c:
        yield c


def _gh_headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_PAT", "")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _lookup_epic_repo(parent_epic_id: int) -> str | None:
    """Best-effort parent epic repo lookup from the ``epics`` table.

    Returns the repo of the epic's first plan step, or ``None`` if the row
    cannot be loaded (no DB configured, psycopg missing, row absent).
    """
    dsn = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        return None
    try:
        import psycopg
    except ImportError:
        return None
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT plan FROM epics WHERE id = %s", (parent_epic_id,))
                row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning("sub_issue: epic repo lookup failed: %s", e)
        return None
    if not row:
        return None
    plan = row[0]
    if isinstance(plan, str):
        import json as _json

        try:
            plan = _json.loads(plan)
        except (ValueError, TypeError):
            return None
    if not isinstance(plan, dict):
        return None
    steps = plan.get("steps") or []
    if not steps:
        return None
    return (steps[0] or {}).get("repo") or None


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
        kind: One of ``capability_request``, ``blocked``,
            ``decision_needed``, ``observation``, ``cross_repo``.
        repo: Override the repo (defaults to the parent epic's first step
            repo).
        mcp_url/mcp_token: Override MCP coords (default to env vars).

    Returns:
        ``{sub_issue_url, sub_issue_number, kind, parent_epic_id,
        repo, ok, side_effect}``.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"Invalid kind: {kind!r}. Expected one of {VALID_KINDS}.")

    resolved_repo = repo or _lookup_epic_repo(parent_epic_id)
    mcp_resolved_url, mcp_resolved_token = _resolve_mcp(mcp_url, mcp_token)

    result: dict[str, Any] = {
        "ok": False,
        "kind": kind,
        "parent_epic_id": parent_epic_id,
        "repo": resolved_repo,
        "sub_issue_url": None,
        "sub_issue_number": None,
        "side_effect": None,
    }

    if not resolved_repo:
        result["error"] = "no_repo_for_parent_epic"
        await _record_event(
            kind=kind,
            parent_epic_id=parent_epic_id,
            sub_issue_url=None,
            repo=None,
            mcp_url=mcp_resolved_url,
            mcp_token=mcp_resolved_token,
            extra={"error": "no_repo_for_parent_epic"},
        )
        return result

    full_body = _compose_body(parent_epic_id, body, kind)
    labels = [_SUB_ISSUE_LABEL, f"kind:{kind}"]

    issue = await _create_github_issue(resolved_repo, title, full_body, labels)
    if not issue:
        result["error"] = "github_issue_create_failed"
        await _record_event(
            kind=kind,
            parent_epic_id=parent_epic_id,
            sub_issue_url=None,
            repo=resolved_repo,
            mcp_url=mcp_resolved_url,
            mcp_token=mcp_resolved_token,
            extra={"error": "github_issue_create_failed"},
        )
        return result

    result["ok"] = True
    result["sub_issue_url"] = issue.get("html_url")
    result["sub_issue_number"] = issue.get("number")

    if kind == "capability_request":
        result["side_effect"] = await _request_capability(
            parent_epic_id=parent_epic_id,
            title=title,
            body=body,
            repo=resolved_repo,
            sub_issue_url=result["sub_issue_url"],
            mcp_url=mcp_resolved_url,
            mcp_token=mcp_resolved_token,
        )
    elif kind == "blocked":
        result["side_effect"] = await _signal_blocked(
            parent_epic_id=parent_epic_id,
            title=title,
            body=body,
            repo=resolved_repo,
            sub_issue_url=result["sub_issue_url"],
            mcp_url=mcp_resolved_url,
            mcp_token=mcp_resolved_token,
        )
    elif kind == "decision_needed":
        result["side_effect"] = _enqueue_decision_move(
            parent_epic_id=parent_epic_id,
            title=title,
            body=body,
            repo=resolved_repo,
            sub_issue_url=result["sub_issue_url"],
        )

    await _record_event(
        kind=kind,
        parent_epic_id=parent_epic_id,
        sub_issue_url=result["sub_issue_url"],
        repo=resolved_repo,
        mcp_url=mcp_resolved_url,
        mcp_token=mcp_resolved_token,
        extra={"sub_issue_number": result["sub_issue_number"]},
    )

    return result


def _compose_body(parent_epic_id: int, body: str, kind: str) -> str:
    header = f"**Parent epic:** #{parent_epic_id}\n**Kind:** `{kind}`\n\n"
    footer = "\n\n_Filed by motto-director sub_issue._"
    return f"{header}{body or ''}{footer}"


async def _create_github_issue(
    repo_full_name: str,
    title: str,
    body: str,
    labels: list[str],
) -> dict[str, Any] | None:
    """Create a GitHub Issue via the REST API. Returns the response dict or
    ``None`` on failure.
    """
    headers = _gh_headers()
    payload = {"title": title, "body": body, "labels": labels}
    url = f"{GITHUB_API}/repos/{repo_full_name}/issues"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, headers=headers, json=payload)
    except Exception as e:  # noqa: BLE001
        logger.warning("sub_issue: GH issue POST failed: %s", e)
        return None
    if r.status_code >= 300:
        logger.warning(
            "sub_issue: GH issue create %s -> %s %s",
            repo_full_name,
            r.status_code,
            r.text[:200],
        )
        return None
    try:
        return r.json()
    except ValueError:
        return None


async def _post_parent_comment(
    parent_epic_id: int,
    sub_issue_url: str,
    mcp_url: str,
    mcp_token: str,
) -> None:
    """Post a comment on the parent epic's GitHub Issue referencing the new
    sub-issue. No-op when the parent epic isn't linked to a GH issue.
    """
    if not (mcp_url and mcp_token and sub_issue_url):
        return
    try:
        async with _mcp_client(mcp_url, mcp_token) as c:
            await c.call_tool(
                "record_event",
                {
                    "agent_name": "motto-director",
                    "kind": "epic.sub_issue.parent_comment",
                    "payload": {
                        "parent_epic_id": parent_epic_id,
                        "sub_issue_url": sub_issue_url,
                    },
                    "level": "info",
                },
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("sub_issue: parent comment record failed: %s", e)


async def _request_capability(
    *,
    parent_epic_id: int,
    title: str,
    body: str,
    repo: str,
    sub_issue_url: str | None,
    mcp_url: str | None,
    mcp_token: str | None,
) -> dict[str, Any]:
    if not (mcp_url and mcp_token):
        return {"ok": False, "skipped": "no_mcp"}
    try:
        async with _mcp_client(mcp_url, mcp_token) as c:
            resp = await c.call_tool(
                "request_capability",
                {
                    "capability": title,
                    "justification": body or sub_issue_url or "",
                    "requested_by": "motto-director",
                    "repo": repo,
                },
            )
        data = getattr(resp, "data", None) or {}
        request_id = data.get("request_id") if isinstance(data, dict) else None
        return {"ok": True, "request_id": request_id}
    except Exception as e:  # noqa: BLE001
        logger.warning("sub_issue: request_capability failed: %s", e)
        return {"ok": False, "error": str(e)}


async def _signal_blocked(
    *,
    parent_epic_id: int,
    title: str,
    body: str,
    repo: str,
    sub_issue_url: str | None,
    mcp_url: str | None,
    mcp_token: str | None,
) -> dict[str, Any]:
    if not (mcp_url and mcp_token):
        return {"ok": False, "skipped": "no_mcp"}
    try:
        async with _mcp_client(mcp_url, mcp_token) as c:
            resp = await c.call_tool(
                "signal_intent",
                {
                    "target_agent": "luke",
                    "kind": "epic_blocked",
                    "payload": {
                        "parent_epic_id": parent_epic_id,
                        "title": title,
                        "body": body,
                        "repo": repo,
                        "sub_issue_url": sub_issue_url,
                    },
                    "source_agent": "motto-director",
                },
            )
        data = getattr(resp, "data", None) or {}
        return {"ok": True, "intent_id": data.get("intent_id") if isinstance(data, dict) else None}
    except Exception as e:  # noqa: BLE001
        logger.warning("sub_issue: signal_intent epic_blocked failed: %s", e)
        return {"ok": False, "error": str(e)}


def _enqueue_decision_move(
    *,
    parent_epic_id: int,
    title: str,
    body: str,
    repo: str,
    sub_issue_url: str | None,
) -> dict[str, Any]:
    """Post the sub-issue in the cockpit pending-moves queue so a human can
    pick an answer. Imported lazily to keep the sub_issue module DB-free
    when only GH side effects are needed.
    """
    try:
        from director.ideate import NextMove
        from director.queue import enqueue_moves, is_configured
    except ImportError as e:
        return {"ok": False, "skipped": f"import_error:{e}"}
    if not is_configured():
        return {"ok": False, "skipped": "no_db"}
    move = NextMove(
        repo=repo,
        kind="file_issue",
        title=f"Decision needed: {title}"[:200],
        rationale=(body or "")[:1000],
        prompt_for_claude_code="",
        priority=2,
        intent=f"Sub-issue {sub_issue_url or ''} on epic #{parent_epic_id}",
        code_changes=[],
        epic_id=parent_epic_id,
    )
    try:
        counts = enqueue_moves([move], run_id=f"sub_issue:{parent_epic_id}")
    except Exception as e:  # noqa: BLE001
        logger.warning("sub_issue: pending_moves enqueue failed: %s", e)
        return {"ok": False, "error": str(e)}
    return {"ok": True, "counts": counts}


async def _record_event(
    *,
    kind: str,
    parent_epic_id: int,
    sub_issue_url: str | None,
    repo: str | None,
    mcp_url: str | None,
    mcp_token: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    if not (mcp_url and mcp_token):
        return
    payload = {
        "parent_epic_id": parent_epic_id,
        "kind": kind,
        "sub_issue_url": sub_issue_url,
        "repo": repo,
    }
    if extra:
        payload.update(extra)
    try:
        async with _mcp_client(mcp_url, mcp_token) as c:
            await c.call_tool(
                "record_event",
                {
                    "agent_name": "motto-director",
                    "kind": "epic.sub_issue.filed",
                    "payload": payload,
                    "level": "info",
                },
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("sub_issue: record_event failed: %s", e)
