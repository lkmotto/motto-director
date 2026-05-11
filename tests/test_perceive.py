"""Tests for perceive.py — all GitHub + Northflank calls are respx-mocked."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import respx

from director.perceive import DEFAULT_WATCH_REPOS, parse_watch_repos, perceive


def test_parse_watch_repos_returns_default_when_unset_or_empty():
    assert parse_watch_repos(None) == DEFAULT_WATCH_REPOS
    assert parse_watch_repos("") == DEFAULT_WATCH_REPOS
    assert parse_watch_repos("   ") == DEFAULT_WATCH_REPOS
    assert parse_watch_repos(" , , ") == DEFAULT_WATCH_REPOS


def test_parse_watch_repos_handles_commas_and_whitespace():
    assert parse_watch_repos("foo/bar,baz/qux") == ("foo/bar", "baz/qux")
    assert parse_watch_repos(" foo/bar , baz/qux ") == ("foo/bar", "baz/qux")
    assert parse_watch_repos("foo/bar,,baz/qux") == ("foo/bar", "baz/qux")
    assert parse_watch_repos("solo/repo") == ("solo/repo",)


def test_default_watch_repos_excludes_downtime_product_line():
    """DownTime repos belong to a separate product line and must never be
    in the Motto director's default watch list."""
    for slug in DEFAULT_WATCH_REPOS:
        name = slug.split("/")[-1]
        assert not name.startswith("downtime-"), (
            f"{slug} is a DownTime repo and must not be in DEFAULT_WATCH_REPOS"
        )


def test_default_watch_repos_covers_revenue_and_appraisal_cores():
    """Sanity: the broadened watch list covers the highest-leverage repos."""
    must_include = {
        "lkmotto/motto-director",
        "lkmotto/motto-mcp-server",
        "lkmotto/motto-sdr-agent",
        "lkmotto/motto-appraisal-pipeline",
        "lkmotto/motto-appraisal-cockpit",
    }
    missing = must_include - set(DEFAULT_WATCH_REPOS)
    assert not missing, f"DEFAULT_WATCH_REPOS missing high-leverage repos: {missing}"


def _iso(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


@respx.mock
def test_perceive_collects_state_for_all_repos():
    repos = ("lkmotto/motto-social-agent",)

    respx.get("https://api.github.com/repos/lkmotto/motto-social-agent").mock(
        return_value=httpx.Response(200, json={"default_branch": "main"})
    )
    respx.get("https://api.github.com/repos/lkmotto/motto-social-agent/branches/main").mock(
        return_value=httpx.Response(
            200,
            json={
                "commit": {
                    "commit": {"committer": {"date": _iso(2.0)}},
                }
            },
        )
    )
    respx.get("https://api.github.com/repos/lkmotto/motto-social-agent/pulls").mock(
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
    respx.get("https://api.github.com/repos/lkmotto/motto-social-agent/pulls/42/reviews").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"user": {"login": "alice"}, "state": "APPROVED"},
            ],
        )
    )
    respx.get("https://api.github.com/repos/lkmotto/motto-social-agent/issues").mock(
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
    respx.get("https://api.northflank.com/v1/projects/motto/jobs/pipeline-auto-nudge/runs").mock(
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

    respx.get("https://api.github.com/repos/lkmotto/motto-sdr-agent").mock(
        return_value=httpx.Response(200, json={"default_branch": "main"})
    )
    respx.get("https://api.github.com/repos/lkmotto/motto-sdr-agent/branches/main").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://api.github.com/repos/lkmotto/motto-sdr-agent/pulls").mock(
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
    respx.get("https://api.github.com/repos/lkmotto/motto-sdr-agent/pulls/1/reviews").mock(
        return_value=httpx.Response(
            200,
            json=[{"user": {"login": "bob"}, "state": "CHANGES_REQUESTED"}],
        )
    )
    respx.get("https://api.github.com/repos/lkmotto/motto-sdr-agent/issues").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get("https://api.northflank.com/v1/projects/motto/jobs/pipeline-auto-nudge/runs").mock(
        return_value=httpx.Response(500)
    )

    snapshot = perceive(repos=repos)
    pr = snapshot.repos[0].open_prs[0]
    assert pr.ci_status == "pending"
    assert pr.review_state == "changes_requested"
    assert pr.approvals == 0
    assert snapshot.repos[0].head_age_hours is None
    assert snapshot.pipeline_auto_nudge.last_run_status is None


@respx.mock
def test_perceive_skips_404_repo_and_keeps_good_one(capsys):
    """A single 404 repo must not abort the whole snapshot — we log it and
    keep going."""
    repos = ("lkmotto/missing-repo", "lkmotto/motto-social-agent")

    # Bad repo: /repos/{repo} 404s. No other endpoints should be hit for it.
    respx.get("https://api.github.com/repos/lkmotto/missing-repo").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    # Good repo: full happy-path mocks.
    respx.get("https://api.github.com/repos/lkmotto/motto-social-agent").mock(
        return_value=httpx.Response(200, json={"default_branch": "main"})
    )
    respx.get("https://api.github.com/repos/lkmotto/motto-social-agent/branches/main").mock(
        return_value=httpx.Response(
            200,
            json={"commit": {"commit": {"committer": {"date": _iso(1.0)}}}},
        )
    )
    respx.get("https://api.github.com/repos/lkmotto/motto-social-agent/pulls").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get("https://api.github.com/repos/lkmotto/motto-social-agent/issues").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get("https://api.northflank.com/v1/projects/motto/jobs/pipeline-auto-nudge/runs").mock(
        return_value=httpx.Response(200, json={"data": {"runs": []}})
    )

    snapshot = perceive(repos=repos)

    assert [r.repo for r in snapshot.repos] == ["lkmotto/motto-social-agent"]

    log_lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    events = [json.loads(ln) for ln in log_lines]
    skipped = [e for e in events if e["event"] == "perceive.repo_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["repo"] == "lkmotto/missing-repo"
    assert skipped[0]["status_code"] == 404
    watch = [e for e in events if e["event"] == "perceive.watch_repos"]
    assert watch and watch[0]["repos"] == list(repos)


@respx.mock
def test_perceive_skips_northflank_when_api_key_missing(monkeypatch, capsys):
    """With no NORTHFLANK_API_KEY/TOKEN set, perceive must NOT call the
    Northflank API (an empty Bearer header crashes httpx) — it should log a
    skip event and return a null pipeline status."""
    monkeypatch.delenv("NORTHFLANK_API_KEY", raising=False)
    monkeypatch.delenv("NORTHFLANK_API_TOKEN", raising=False)

    repos = ("lkmotto/motto-social-agent",)
    respx.get("https://api.github.com/repos/lkmotto/motto-social-agent").mock(
        return_value=httpx.Response(200, json={"default_branch": "main"})
    )
    respx.get("https://api.github.com/repos/lkmotto/motto-social-agent/branches/main").mock(
        return_value=httpx.Response(
            200,
            json={"commit": {"commit": {"committer": {"date": _iso(1.0)}}}},
        )
    )
    respx.get("https://api.github.com/repos/lkmotto/motto-social-agent/pulls").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get("https://api.github.com/repos/lkmotto/motto-social-agent/issues").mock(
        return_value=httpx.Response(200, json=[])
    )
    # No respx mock for the Northflank URL — if the guard regresses, respx
    # would raise on the unmocked call and this test would fail.

    snapshot = perceive(repos=repos)

    assert snapshot.pipeline_auto_nudge is not None
    assert snapshot.pipeline_auto_nudge.last_run_status is None
    assert snapshot.pipeline_auto_nudge.last_run_at is None

    log_lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    events = [json.loads(ln) for ln in log_lines]
    skipped = [e for e in events if e["event"] == "perceive.northflank_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "no_api_key"
