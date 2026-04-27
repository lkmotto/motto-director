"""Tests for perceive.py — all GitHub + Northflank calls are respx-mocked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import respx

from director.perceive import perceive


def _iso(hours_ago: float) -> str:
    return (
        datetime.now(UTC) - timedelta(hours=hours_ago)
    ).isoformat().replace("+00:00", "Z")


@respx.mock
def test_perceive_collects_state_for_all_repos():
    repos = ("lkmotto/motto-social-agent",)

    respx.get(
        "https://api.github.com/repos/lkmotto/motto-social-agent"
    ).mock(return_value=httpx.Response(200, json={"default_branch": "main"}))
    respx.get(
        "https://api.github.com/repos/lkmotto/motto-social-agent/branches/main"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "commit": {
                    "commit": {"committer": {"date": _iso(2.0)}},
                }
            },
        )
    )
    respx.get(
        "https://api.github.com/repos/lkmotto/motto-social-agent/pulls"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 42,
                    "title": "Add LinkedIn DM channel",
                    "html_url": "https://github.com/lkmotto/motto-social-agent/pull/42",
                    "created_at": _iso(8.0),
                    "head": {"sha": "abc123"},
                    "labels": [{"name": "auto-merge-ok"}],
                }
            ],
        )
    )
    respx.get(
        "https://api.github.com/repos/lkmotto/motto-social-agent/commits/abc123/check-runs"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"check_runs": [{"conclusion": "success"}]},
        )
    )
    respx.get(
        "https://api.github.com/repos/lkmotto/motto-social-agent/pulls/42/reviews"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"user": {"login": "alice"}, "state": "APPROVED"},
            ],
        )
    )
    respx.get(
        "https://api.github.com/repos/lkmotto/motto-social-agent/issues"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 7,
                    "title": "Bug: drip cadence skips weekends",
                    "html_url": "https://github.com/lkmotto/motto-social-agent/issues/7",
                    "created_at": _iso(48.0),
                    "labels": [{"name": "bug"}],
                },
                # PR-shaped /issues entry should be filtered out
                {
                    "number": 42,
                    "title": "Add LinkedIn DM channel",
                    "pull_request": {"url": "..."},
                    "created_at": _iso(8.0),
                    "html_url": "...",
                    "labels": [],
                },
            ],
        )
    )
    respx.get(
        "https://api.northflank.com/v1/projects/motto/jobs/pipeline-auto-nudge/runs"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "runs": [
                        {"createdAt": _iso(0.5), "status": "succeeded"},
                    ]
                }
            },
        )
    )

    snapshot = perceive(repos=repos)

    assert len(snapshot.repos) == 1
    state = snapshot.repos[0]
    assert state.repo == "lkmotto/motto-social-agent"
    assert state.default_branch == "main"
    assert state.head_age_hours is not None and 1.5 < state.head_age_hours < 2.5

    assert len(state.open_prs) == 1
    pr = state.open_prs[0]
    assert pr.number == 42
    assert pr.ci_status == "success"
    assert pr.review_state == "approved"
    assert pr.approvals == 1
    assert "auto-merge-ok" in pr.labels

    assert len(state.open_issues) == 1
    assert state.open_issues[0].number == 7

    assert snapshot.pipeline_auto_nudge is not None
    assert snapshot.pipeline_auto_nudge.last_run_status == "succeeded"


@respx.mock
def test_perceive_handles_pending_ci_and_changes_requested():
    repos = ("lkmotto/motto-sdr-agent",)

    respx.get(
        "https://api.github.com/repos/lkmotto/motto-sdr-agent"
    ).mock(return_value=httpx.Response(200, json={"default_branch": "main"}))
    respx.get(
        "https://api.github.com/repos/lkmotto/motto-sdr-agent/branches/main"
    ).mock(return_value=httpx.Response(404))
    respx.get(
        "https://api.github.com/repos/lkmotto/motto-sdr-agent/pulls"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 1,
                    "title": "WIP",
                    "html_url": "x",
                    "created_at": _iso(1.0),
                    "head": {"sha": "deadbeef"},
                    "labels": [],
                }
            ],
        )
    )
    respx.get(
        "https://api.github.com/repos/lkmotto/motto-sdr-agent/commits/deadbeef/check-runs"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"check_runs": [{"conclusion": None}, {"conclusion": "success"}]},
        )
    )
    respx.get(
        "https://api.github.com/repos/lkmotto/motto-sdr-agent/pulls/1/reviews"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{"user": {"login": "bob"}, "state": "CHANGES_REQUESTED"}],
        )
    )
    respx.get(
        "https://api.github.com/repos/lkmotto/motto-sdr-agent/issues"
    ).mock(return_value=httpx.Response(200, json=[]))
    respx.get(
        "https://api.northflank.com/v1/projects/motto/jobs/pipeline-auto-nudge/runs"
    ).mock(return_value=httpx.Response(500))

    snapshot = perceive(repos=repos)
    pr = snapshot.repos[0].open_prs[0]
    assert pr.ci_status == "pending"
    assert pr.review_state == "changes_requested"
    assert pr.approvals == 0
    assert snapshot.repos[0].head_age_hours is None
    assert snapshot.pipeline_auto_nudge.last_run_status is None
