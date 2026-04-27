"""Perceive: collect state across the motto stack into a typed Snapshot."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

GITHUB_API = "https://api.github.com"
NORTHFLANK_API = "https://api.northflank.com/v1"

# Verified-real slugs under lkmotto (confirmed via GitHub org search). Override
# at deploy time by setting WATCH_REPOS to a comma-separated list.
DEFAULT_WATCH_REPOS: tuple[str, ...] = (
    "lkmotto/motto-conductor",
    "lkmotto/motto-social-agent",
    "lkmotto/motto-sdr-agent",
    "lkmotto/motto-appraisal-pipeline",
    "lkmotto/motto-appraisal-cockpit",
)


def parse_watch_repos(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated WATCH_REPOS env value. Whitespace around each
    slug is trimmed; empty/missing falls back to DEFAULT_WATCH_REPOS."""
    if raw is None:
        return DEFAULT_WATCH_REPOS
    parts = tuple(s.strip() for s in raw.split(",") if s.strip())
    return parts or DEFAULT_WATCH_REPOS


REPOS: tuple[str, ...] = parse_watch_repos(os.environ.get("WATCH_REPOS"))

NORTHFLANK_PROJECT = os.environ.get("NORTHFLANK_PROJECT", "motto")
PIPELINE_AUTO_NUDGE_JOB = os.environ.get(
    "PIPELINE_AUTO_NUDGE_JOB", "pipeline-auto-nudge"
)


@dataclass
class PullRequest:
    repo: str
    number: int
    title: str
    url: str
    age_hours: float
    ci_status: str  # "success" | "failure" | "pending" | "unknown"
    review_state: str  # "approved" | "changes_requested" | "pending"
    approvals: int
    labels: list[str]
    head_sha: str


@dataclass
class Issue:
    repo: str
    number: int
    title: str
    url: str
    age_hours: float
    labels: list[str]


@dataclass
class RepoState:
    repo: str
    open_prs: list[PullRequest] = field(default_factory=list)
    open_issues: list[Issue] = field(default_factory=list)
    default_branch: str = "main"
    head_age_hours: float | None = None


@dataclass
class NorthflankJobStatus:
    job: str
    last_run_at: str | None
    last_run_status: str | None  # "succeeded" | "failed" | "running" | None


@dataclass
class Snapshot:
    captured_at: str
    repos: list[RepoState] = field(default_factory=list)
    pipeline_auto_nudge: NorthflankJobStatus | None = None


def _log(event: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


def _age_hours(iso: str) -> float:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(UTC) - dt).total_seconds() / 3600.0


def _gh_headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def northflank_api_key() -> str:
    """Read Northflank credential. Prefers NORTHFLANK_API_KEY (per shared
    sdr-agent-secrets group); falls back to NORTHFLANK_API_TOKEN."""
    return os.environ.get("NORTHFLANK_API_KEY") or os.environ.get(
        "NORTHFLANK_API_TOKEN", ""
    )


def _nf_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {northflank_api_key()}",
        "Accept": "application/json",
    }


def _fetch_pr_ci_status(client: httpx.Client, repo: str, sha: str) -> str:
    r = client.get(
        f"{GITHUB_API}/repos/{repo}/commits/{sha}/check-runs",
        headers=_gh_headers(),
    )
    if r.status_code != 200:
        return "unknown"
    runs = r.json().get("check_runs", [])
    if not runs:
        return "unknown"
    conclusions = {run.get("conclusion") for run in runs}
    if None in conclusions or "neutral" in conclusions:
        return "pending"
    if "failure" in conclusions or "timed_out" in conclusions or "cancelled" in conclusions:
        return "failure"
    if conclusions == {"success"} or conclusions == {"success", "skipped"}:
        return "success"
    return "pending"


def _fetch_pr_reviews(
    client: httpx.Client, repo: str, number: int
) -> tuple[str, int]:
    r = client.get(
        f"{GITHUB_API}/repos/{repo}/pulls/{number}/reviews",
        headers=_gh_headers(),
    )
    if r.status_code != 200:
        return "pending", 0
    latest_by_user: dict[str, str] = {}
    for review in r.json():
        user = review.get("user", {}).get("login")
        state = review.get("state")
        if user and state in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}:
            latest_by_user[user] = state
    approvals = sum(1 for s in latest_by_user.values() if s == "APPROVED")
    if any(s == "CHANGES_REQUESTED" for s in latest_by_user.values()):
        return "changes_requested", approvals
    if approvals > 0:
        return "approved", approvals
    return "pending", approvals


def _fetch_repo_prs(client: httpx.Client, repo: str) -> list[PullRequest]:
    r = client.get(
        f"{GITHUB_API}/repos/{repo}/pulls",
        params={"state": "open", "per_page": 50},
        headers=_gh_headers(),
    )
    r.raise_for_status()
    out: list[PullRequest] = []
    for pr in r.json():
        sha = pr["head"]["sha"]
        ci = _fetch_pr_ci_status(client, repo, sha)
        review_state, approvals = _fetch_pr_reviews(client, repo, pr["number"])
        out.append(
            PullRequest(
                repo=repo,
                number=pr["number"],
                title=pr["title"],
                url=pr["html_url"],
                age_hours=_age_hours(pr["created_at"]),
                ci_status=ci,
                review_state=review_state,
                approvals=approvals,
                labels=[lbl["name"] for lbl in pr.get("labels", [])],
                head_sha=sha,
            )
        )
    return out


def _fetch_repo_issues(client: httpx.Client, repo: str) -> list[Issue]:
    r = client.get(
        f"{GITHUB_API}/repos/{repo}/issues",
        params={"state": "open", "per_page": 50},
        headers=_gh_headers(),
    )
    r.raise_for_status()
    out: list[Issue] = []
    for issue in r.json():
        if "pull_request" in issue:
            continue  # /issues includes PRs; skip them
        out.append(
            Issue(
                repo=repo,
                number=issue["number"],
                title=issue["title"],
                url=issue["html_url"],
                age_hours=_age_hours(issue["created_at"]),
                labels=[lbl["name"] for lbl in issue.get("labels", [])],
            )
        )
    return out


def _fetch_default_branch_head(
    client: httpx.Client, repo: str
) -> tuple[str, float | None] | None:
    """Probe the repo. Returns (default_branch, head_age_hours) on success,
    or None if the repo is missing/unreachable — a single bad repo must not
    abort the whole snapshot."""
    try:
        r = client.get(f"{GITHUB_API}/repos/{repo}", headers=_gh_headers())
        r.raise_for_status()
        default_branch = r.json().get("default_branch", "main")
        rb = client.get(
            f"{GITHUB_API}/repos/{repo}/branches/{default_branch}",
            headers=_gh_headers(),
        )
        if rb.status_code != 200:
            return default_branch, None
        commit = rb.json().get("commit", {}).get("commit", {})
        iso = (
            commit.get("committer", {}).get("date")
            or commit.get("author", {}).get("date")
        )
        return default_branch, _age_hours(iso) if iso else None
    except httpx.HTTPStatusError as exc:
        _log(
            "perceive.repo_skipped",
            repo=repo,
            status_code=exc.response.status_code,
            reason=exc.response.reason_phrase or "http_error",
        )
        return None
    except httpx.RequestError as exc:
        _log(
            "perceive.repo_skipped",
            repo=repo,
            status_code=None,
            reason=f"{type(exc).__name__}: {exc}",
        )
        return None


def _fetch_northflank_job(
    client: httpx.Client, project: str, job: str
) -> NorthflankJobStatus:
    r = client.get(
        f"{NORTHFLANK_API}/projects/{project}/jobs/{job}/runs",
        params={"per_page": 1},
        headers=_nf_headers(),
    )
    if r.status_code != 200:
        return NorthflankJobStatus(job=job, last_run_at=None, last_run_status=None)
    runs = r.json().get("data", {}).get("runs", []) or r.json().get("runs", [])
    if not runs:
        return NorthflankJobStatus(job=job, last_run_at=None, last_run_status=None)
    last = runs[0]
    return NorthflankJobStatus(
        job=job,
        last_run_at=last.get("createdAt") or last.get("startedAt"),
        last_run_status=last.get("status"),
    )


def perceive(repos: tuple[str, ...] | None = None) -> Snapshot:
    """Collect a Snapshot of the motto stack. If `repos` is not given, the
    watch list is resolved from the WATCH_REPOS env (comma-separated),
    falling back to DEFAULT_WATCH_REPOS."""
    if repos is None:
        repos = parse_watch_repos(os.environ.get("WATCH_REPOS"))
    _log("perceive.watch_repos", repos=list(repos))
    captured_at = datetime.now(UTC).isoformat()
    repo_states: list[RepoState] = []
    pipeline_status: NorthflankJobStatus | None = None

    with httpx.Client(timeout=30.0) as client:
        for repo in repos:
            head = _fetch_default_branch_head(client, repo)
            if head is None:
                continue  # 404 / unreachable — skipped + logged inside helper
            default_branch, head_age = head
            repo_states.append(
                RepoState(
                    repo=repo,
                    open_prs=_fetch_repo_prs(client, repo),
                    open_issues=_fetch_repo_issues(client, repo),
                    default_branch=default_branch,
                    head_age_hours=head_age,
                )
            )
        pipeline_status = _fetch_northflank_job(
            client, NORTHFLANK_PROJECT, PIPELINE_AUTO_NUDGE_JOB
        )

    return Snapshot(
        captured_at=captured_at,
        repos=repo_states,
        pipeline_auto_nudge=pipeline_status,
    )
