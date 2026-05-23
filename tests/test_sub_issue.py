"""Unit tests for director.sub_issue.

DB-free + GitHub-free + MCP-free: every external call is monkeypatched.
The tests focus on kind routing — each kind must trigger the right
secondary side effect and the always-on record_event.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from director import sub_issue


class _FakeMCPClient:
    """Stand-in for the fastmcp Client returned by ``_mcp_client``.

    Captures every ``call_tool`` invocation so tests can assert on the
    routing logic.
    """

    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self._calls = calls

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self._calls.append((name, args))

        class _Resp:
            data = {"intent_id": "intent-1", "request_id": "req-1", "event_id": 1}

        return _Resp()


@pytest.fixture
def mcp_calls(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    @asynccontextmanager
    async def _fake_client(url: str, token: str):
        yield _FakeMCPClient(calls)

    monkeypatch.setattr(sub_issue, "_mcp_client", _fake_client)
    return calls


@pytest.fixture
def fake_gh(monkeypatch):
    captured: dict[str, Any] = {}

    async def _fake_create_github_issue(repo, title, body, labels):
        captured["repo"] = repo
        captured["title"] = title
        captured["body"] = body
        captured["labels"] = labels
        return {
            "number": 999,
            "html_url": f"https://github.com/{repo}/issues/999",
        }

    monkeypatch.setattr(sub_issue, "_create_github_issue", _fake_create_github_issue)
    return captured


@pytest.fixture(autouse=True)
def _mcp_env(monkeypatch):
    monkeypatch.setenv("MOTTO_MCP_URL", "https://mcp.example/test")
    monkeypatch.setenv("MOTTO_MCP_AUTH_TOKEN", "tok")
    yield


async def test_invalid_kind_raises():
    with pytest.raises(ValueError):
        await sub_issue.file_sub_issue(
            parent_epic_id=1,
            title="x",
            body="y",
            kind="bogus",
            repo="lkmotto/x",
        )


async def test_capability_request_routes_and_records(fake_gh, mcp_calls):
    result = await sub_issue.file_sub_issue(
        parent_epic_id=7,
        title="Need GH PAT for org X",
        body="we need org admin scope",
        kind="capability_request",
        repo="lkmotto/motto-director",
    )
    assert result["ok"] is True
    assert result["sub_issue_number"] == 999
    assert "kind:capability_request" in fake_gh["labels"]
    assert "sub-issue" in fake_gh["labels"]
    # Parent epic link is prepended to body.
    assert "Parent epic: #7" in fake_gh["body"] or "#7" in fake_gh["body"]
    # MCP routing: request_capability + record_event (both fire).
    tool_names = [c[0] for c in mcp_calls]
    assert "request_capability" in tool_names
    assert "record_event" in tool_names
    rc_args = next(args for n, args in mcp_calls if n == "request_capability")
    assert rc_args["capability"] == "Need GH PAT for org X"
    assert rc_args["repo"] == "lkmotto/motto-director"
    rec_args = next(args for n, args in mcp_calls if n == "record_event")
    assert rec_args["kind"] == "epic.sub_issue.filed"
    assert rec_args["payload"]["kind"] == "capability_request"


async def test_blocked_signals_intent_to_luke(fake_gh, mcp_calls):
    result = await sub_issue.file_sub_issue(
        parent_epic_id=11,
        title="Blocked: missing migration",
        body="cannot proceed without 0008",
        kind="blocked",
        repo="lkmotto/motto-director",
    )
    assert result["ok"] is True
    tool_names = [c[0] for c in mcp_calls]
    assert "signal_intent" in tool_names
    assert "record_event" in tool_names
    intent_args = next(args for n, args in mcp_calls if n == "signal_intent")
    assert intent_args["target_agent"] == "luke"
    assert intent_args["kind"] == "epic_blocked"
    assert intent_args["payload"]["parent_epic_id"] == 11
    assert intent_args["payload"]["sub_issue_url"].endswith("/issues/999")


async def test_decision_needed_enqueues_pending_move(fake_gh, mcp_calls, monkeypatch):
    captured_moves: list[Any] = []

    def _fake_enqueue(moves, run_id):
        captured_moves.extend(moves)
        return {"queued": len(moves), "deduped": 0, "errors": 0}

    monkeypatch.setattr("director.queue.is_configured", lambda: True)
    monkeypatch.setattr("director.queue.enqueue_moves", _fake_enqueue)

    result = await sub_issue.file_sub_issue(
        parent_epic_id=21,
        title="Pick provider: groq vs deepseek?",
        body="cost vs latency tradeoff",
        kind="decision_needed",
        repo="lkmotto/motto-director",
    )
    assert result["ok"] is True
    assert result["side_effect"]["ok"] is True
    assert result["side_effect"]["counts"]["queued"] == 1
    # Move payload references the epic + carries the title.
    move = captured_moves[0]
    assert move.epic_id == 21
    assert move.repo == "lkmotto/motto-director"
    assert "Pick provider" in move.title
    # Event still recorded.
    tool_names = [c[0] for c in mcp_calls]
    assert "record_event" in tool_names
    # No request_capability / signal_intent for this kind.
    assert "request_capability" not in tool_names
    assert "signal_intent" not in tool_names


async def test_observation_records_event_only(fake_gh, mcp_calls):
    result = await sub_issue.file_sub_issue(
        parent_epic_id=33,
        title="Observation: CI flaked twice today",
        body="flaky test_phase4_epics_seed",
        kind="observation",
        repo="lkmotto/motto-director",
    )
    assert result["ok"] is True
    assert result["side_effect"] is None
    tool_names = [c[0] for c in mcp_calls]
    # Only record_event — no routing side effect.
    assert tool_names == ["record_event"]
    assert "kind:observation" in fake_gh["labels"]


async def test_cross_repo_legacy_kind_accepted(fake_gh, mcp_calls):
    result = await sub_issue.file_sub_issue(
        parent_epic_id=2,
        title="Touches motto-mcp-server too",
        body="need a coordinated bump",
        kind="cross_repo",
        repo="lkmotto/motto-director",
    )
    assert result["ok"] is True
    assert "kind:cross_repo" in fake_gh["labels"]
    tool_names = [c[0] for c in mcp_calls]
    # Legacy kind: only the always-on record_event fires.
    assert tool_names == ["record_event"]


async def test_repo_defaults_to_parent_epic(monkeypatch, fake_gh, mcp_calls):
    monkeypatch.setattr(sub_issue, "_lookup_epic_repo", lambda eid: "lkmotto/from-db")
    result = await sub_issue.file_sub_issue(
        parent_epic_id=44,
        title="auto-resolved repo",
        body="x",
        kind="observation",
    )
    assert result["repo"] == "lkmotto/from-db"
    assert fake_gh["repo"] == "lkmotto/from-db"
    assert result["ok"] is True


async def test_missing_repo_returns_not_ok(monkeypatch, mcp_calls):
    monkeypatch.setattr(sub_issue, "_lookup_epic_repo", lambda eid: None)

    async def _should_not_be_called(*a, **kw):
        raise AssertionError("GH issue should not be created when no repo")

    monkeypatch.setattr(sub_issue, "_create_github_issue", _should_not_be_called)
    result = await sub_issue.file_sub_issue(
        parent_epic_id=55,
        title="x",
        body="y",
        kind="observation",
    )
    assert result["ok"] is False
    assert result["error"] == "no_repo_for_parent_epic"
    # Event still recorded with the error.
    rec = next(args for n, args in mcp_calls if n == "record_event")
    assert rec["payload"]["error"] == "no_repo_for_parent_epic"


async def test_github_failure_returns_not_ok(monkeypatch, mcp_calls):
    async def _fail_create(repo, title, body, labels):
        return None

    monkeypatch.setattr(sub_issue, "_create_github_issue", _fail_create)
    result = await sub_issue.file_sub_issue(
        parent_epic_id=66,
        title="x",
        body="y",
        kind="capability_request",
        repo="lkmotto/x",
    )
    assert result["ok"] is False
    assert result["error"] == "github_issue_create_failed"
    # When the issue creation fails we should not also call request_capability.
    tool_names = [c[0] for c in mcp_calls]
    assert "request_capability" not in tool_names
    # We still record the failure event.
    assert "record_event" in tool_names
