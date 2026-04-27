"""Tests for ideate.py — Anthropic client mocked with a fixture response."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from director.ideate import ideate
from director.perceive import (
    Issue,
    NorthflankJobStatus,
    PullRequest,
    RepoState,
    Snapshot,
)


def _fake_anthropic(payload: dict | str) -> MagicMock:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
    )
    client = MagicMock()
    client.messages.create.return_value = response
    return client


def _snapshot() -> Snapshot:
    return Snapshot(
        captured_at="2026-04-27T00:00:00Z",
        repos=[
            RepoState(
                repo="lkmotto/motto-appraisal-pipeline",
                open_prs=[
                    PullRequest(
                        repo="lkmotto/motto-appraisal-pipeline",
                        number=12,
                        title="Bump pyarrow",
                        url="x",
                        age_hours=72.0,
                        ci_status="success",
                        review_state="approved",
                        approvals=1,
                        labels=["auto-merge-ok"],
                        head_sha="sha1",
                    )
                ],
                open_issues=[
                    Issue(
                        repo="lkmotto/motto-appraisal-pipeline",
                        number=99,
                        title="Cockpit cannot reach pipeline",
                        url="x",
                        age_hours=200.0,
                        labels=["bug", "P1"],
                    )
                ],
                default_branch="main",
                head_age_hours=24.0,
            )
        ],
        pipeline_auto_nudge=NorthflankJobStatus(
            job="pipeline-auto-nudge",
            last_run_at="2026-04-26T00:00:00Z",
            last_run_status="failed",
        ),
    )


def test_ideate_parses_moves_and_keeps_only_those_with_intent():
    payload = {
        "moves": [
            {
                "repo": "lkmotto/motto-appraisal-pipeline",
                "kind": "merge_pr",
                "title": "Bump pyarrow",
                "rationale": "Trivial dependency bump, CI green, approved.",
                "prompt_for_claude_code": "",
                "priority": 1,
                "intent": (
                    "PR #12 has been green and approved for 72h with "
                    "auto-merge-ok; merging unblocks the pipeline release."
                ),
            },
            {
                "repo": "lkmotto/motto-appraisal-pipeline",
                "kind": "spawn_session",
                "title": "Investigate cockpit→pipeline outage",
                "rationale": "P1 bug, 200h old.",
                "prompt_for_claude_code": "Reproduce issue #99 and propose a fix.",
                "priority": 2,
                "intent": (
                    "Issue #99 (P1) has been open 200h with no PR; "
                    "pipeline-auto-nudge job last failed, suggesting "
                    "connectivity is still broken."
                ),
            },
            {
                "repo": "lkmotto/motto-appraisal-pipeline",
                "kind": "nudge_pipeline",
                "title": "Retry pipeline tick",
                "rationale": "Last run failed.",
                "prompt_for_claude_code": "",
                "priority": 3,
                # missing intent → must be dropped
            },
            {
                "repo": "lkmotto/motto-appraisal-pipeline",
                "kind": "noop",
                "title": "",
                "rationale": "",
                "prompt_for_claude_code": "",
                "priority": 5,
                "intent": "Everything else is healthy.",
            },
        ]
    }
    client = _fake_anthropic(payload)

    moves = ideate(_snapshot(), client=client)

    assert [m.kind for m in moves] == ["merge_pr", "spawn_session", "noop"]
    assert moves[0].priority == 1
    assert moves[1].prompt_for_claude_code.startswith("Reproduce")
    assert all(m.intent for m in moves)


def test_ideate_handles_fenced_json_response():
    payload = (
        "```json\n"
        + json.dumps(
            {
                "moves": [
                    {
                        "repo": "lkmotto/motto-sdr-agent",
                        "kind": "file_issue",
                        "title": "Drip cadence regression",
                        "rationale": "weekend skip",
                        "prompt_for_claude_code": "",
                        "priority": 4,
                        "intent": (
                            "No PR addresses the weekend-skip cadence "
                            "regression; filing an issue captures it."
                        ),
                    }
                ]
            }
        )
        + "\n```"
    )
    client = _fake_anthropic(payload)
    moves = ideate(_snapshot(), client=client)
    assert len(moves) == 1
    assert moves[0].kind == "file_issue"


def test_ideate_returns_empty_on_unparseable_response():
    client = _fake_anthropic("not json at all")
    assert ideate(_snapshot(), client=client) == []
