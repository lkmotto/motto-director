"""Deep-read step: before lenses ideate, fetch *actual content* of the
top-signal PRs / issues / files in the snapshot.

The default Snapshot only contains metadata (titles, ages, CI statuses).
That's fine for janitorial moves but useless for architectural reasoning,
because the director can't see what code or copy is actually changing.

This module fetches PR diffs, issue bodies, and key repo files, and
formats them into a `=== REPO EVIDENCE ===` block that gets prepended to
every lens's user message. Lenses can then propose moves grounded in
real source, not just inferred from titles.

Levels (controlled by DIRECTOR_DEEP_READ_LEVEL):
  off    \u2014 disabled (default; preserves legacy behavior)
  light  \u2014 top 5 PR diffs (truncated) + top 3 issue bodies. ~30k tokens.
  medium \u2014 top 10 PR diffs + top 6 issue bodies + per-repo CLAUDE.md
           + per-repo README.md (truncated). ~150k tokens.
  heavy  \u2014 medium + last 10 commits per repo + every changed file path's
           full contents on top 5 PRs. ~600k tokens.

All fetches are best-effort and time-boxed; failures degrade gracefully.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

import httpx

from director.perceive import GITHUB_API, Issue, PullRequest, Snapshot, _gh_headers

logger = logging.getLogger("director.deep_read")

DEEP_READ_TIMEOUT_S = float(os.environ.get("DIRECTOR_DEEP_READ_TIMEOUT_S", "30"))
PR_DIFF_MAX_BYTES = int(os.environ.get("DIRECTOR_PR_DIFF_MAX_BYTES", "8000"))
FILE_MAX_BYTES = int(os.environ.get("DIRECTOR_FILE_MAX_BYTES", "6000"))


def _log(event: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


def _level() -> str:
    return os.environ.get("DIRECTOR_DEEP_READ_LEVEL", "off").strip().lower()


def is_enabled() -> bool:
    return _level() in ("light", "medium", "heavy")


def _budget(level: str) -> dict[str, int]:
    """Per-level fetch budgets."""
    if level == "light":
        return {"pr_diffs": 5, "issue_bodies": 3, "claude_md": 0, "readmes": 0,
                "commits": 0, "pr_files_full": 0}
    if level == "medium":
        return {"pr_diffs": 10, "issue_bodies": 6, "claude_md": 1, "readmes": 1,
                "commits": 0, "pr_files_full": 0}
    if level == "heavy":
        return {"pr_diffs": 10, "issue_bodies": 6, "claude_md": 1, "readmes": 1,
                "commits": 10, "pr_files_full": 5}
    return {}


def _truncate(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n... [truncated]"


def _rank_prs(snapshot: Snapshot) -> list[PullRequest]:
    """Highest-signal PRs first. Heuristics:
      1. failed CI (most actionable)
      2. approved + idle (closest to merge)
      3. youngest PR (most recent context)
    """
    prs: list[PullRequest] = [pr for r in snapshot.repos for pr in r.open_prs]

    def score(pr: PullRequest) -> tuple[int, float]:
        bucket = 3
        if pr.ci_status == "failure":
            bucket = 0
        elif pr.review_state == "approved":
            bucket = 1
        elif pr.age_hours < 24:
            bucket = 2
        return (bucket, pr.age_hours)

    prs.sort(key=score)
    return prs


def _rank_issues(snapshot: Snapshot) -> list[Issue]:
    """Highest-signal issues first. Heuristics:
      1. labeled 'priority' or 'bug' or 'claude-task'
      2. youngest (most recent context)
    """
    issues: list[Issue] = [iss for r in snapshot.repos for iss in r.open_issues]

    def score(iss: Issue) -> tuple[int, float]:
        labels = {lbl.lower() for lbl in iss.labels}
        if labels & {"priority", "high-priority", "claude-task", "director-ok"}:
            return (0, iss.age_hours)
        if "bug" in labels:
            return (1, iss.age_hours)
        return (2, iss.age_hours)

    issues.sort(key=score)
    return issues


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def _fetch_pr_diff(client: httpx.Client, repo: str, number: int) -> str:
    """Fetch a PR's unified diff via the GitHub REST API."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{number}"
    headers = _gh_headers()
    headers["Accept"] = "application/vnd.github.v3.diff"
    try:
        resp = client.get(url, headers=headers, timeout=DEEP_READ_TIMEOUT_S)
        resp.raise_for_status()
        return _truncate(resp.text, PR_DIFF_MAX_BYTES)
    except Exception as exc:  # noqa: BLE001
        return f"[diff fetch failed: {type(exc).__name__}]"


def _fetch_pr_files(
    client: httpx.Client, repo: str, number: int, max_files: int = 5,
) -> list[dict[str, str]]:
    """Fetch the changed-files list (path + full content of new version)."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{number}/files"
    try:
        resp = client.get(url, headers=_gh_headers(), timeout=DEEP_READ_TIMEOUT_S)
        resp.raise_for_status()
        files = resp.json()[:max_files]
    except Exception as exc:  # noqa: BLE001
        return [{"path": "[files fetch failed]", "content": str(exc)[:200]}]

    out: list[dict[str, str]] = []
    for f in files:
        path = f.get("filename", "")
        raw_url = f.get("raw_url") or f.get("contents_url")
        content = ""
        if raw_url:
            try:
                rr = client.get(raw_url, headers=_gh_headers(), timeout=DEEP_READ_TIMEOUT_S)
                if rr.status_code == 200:
                    content = _truncate(rr.text, FILE_MAX_BYTES)
            except Exception as exc:  # noqa: BLE001
                content = f"[file fetch failed: {type(exc).__name__}]"
        out.append({"path": path, "content": content})
    return out


def _fetch_issue_body(client: httpx.Client, repo: str, number: int) -> str:
    url = f"{GITHUB_API}/repos/{repo}/issues/{number}"
    try:
        resp = client.get(url, headers=_gh_headers(), timeout=DEEP_READ_TIMEOUT_S)
        resp.raise_for_status()
        body = resp.json().get("body") or ""
        return _truncate(body, FILE_MAX_BYTES)
    except Exception as exc:  # noqa: BLE001
        return f"[issue body fetch failed: {type(exc).__name__}]"


def _fetch_file(
    client: httpx.Client, repo: str, path: str, ref: str = "main",
) -> str:
    """Fetch a file's raw content from main."""
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}?ref={ref}"
    headers = _gh_headers()
    headers["Accept"] = "application/vnd.github.v3.raw"
    try:
        resp = client.get(url, headers=headers, timeout=DEEP_READ_TIMEOUT_S)
        if resp.status_code != 200:
            return ""
        return _truncate(resp.text, FILE_MAX_BYTES)
    except Exception:  # noqa: BLE001
        return ""


def _fetch_recent_commits(
    client: httpx.Client, repo: str, n: int = 10,
) -> list[dict[str, str]]:
    url = f"{GITHUB_API}/repos/{repo}/commits?per_page={n}"
    try:
        resp = client.get(url, headers=_gh_headers(), timeout=DEEP_READ_TIMEOUT_S)
        resp.raise_for_status()
        out: list[dict[str, str]] = []
        for c in resp.json():
            out.append({
                "sha": (c.get("sha") or "")[:8],
                "msg": ((c.get("commit", {}) or {}).get("message") or "").splitlines()[0][:200],
                "author": ((c.get("commit", {}) or {}).get("author", {}) or {}).get("name", ""),
                "date": ((c.get("commit", {}) or {}).get("author", {}) or {}).get("date", ""),
            })
        return out
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def gather_evidence(snapshot: Snapshot) -> str:
    """Return a `=== REPO EVIDENCE ===` block, or empty string if disabled."""
    level = _level()
    if level not in ("light", "medium", "heavy"):
        return ""

    budget = _budget(level)
    started = datetime.now(UTC)
    sections: list[str] = []

    with httpx.Client(timeout=DEEP_READ_TIMEOUT_S) as client:
        # PR diffs (top N by signal)
        prs = _rank_prs(snapshot)[: budget["pr_diffs"]]
        for pr in prs:
            diff = _fetch_pr_diff(client, pr.repo, pr.number)
            section = (
                f"--- PR {pr.repo}#{pr.number} \"{pr.title}\" "
                f"(age={pr.age_hours:.1f}h, ci={pr.ci_status}, "
                f"review={pr.review_state})\n{diff}\n"
            )
            sections.append(section)

        # Optional: per-PR full file contents (heavy only)
        if budget.get("pr_files_full", 0):
            for pr in prs[: budget["pr_files_full"]]:
                files = _fetch_pr_files(client, pr.repo, pr.number, max_files=5)
                for f in files:
                    sections.append(
                        f"--- FULL FILE on PR {pr.repo}#{pr.number}: "
                        f"{f['path']}\n{f['content']}\n"
                    )

        # Issue bodies
        issues = _rank_issues(snapshot)[: budget["issue_bodies"]]
        for iss in issues:
            body = _fetch_issue_body(client, iss.repo, iss.number)
            sections.append(
                f"--- ISSUE {iss.repo}#{iss.number} \"{iss.title}\" "
                f"(age={iss.age_hours:.1f}h, labels={iss.labels})\n{body}\n"
            )

        # Per-repo CLAUDE.md + README (medium+heavy)
        for repo_state in snapshot.repos:
            if budget.get("claude_md", 0):
                content = _fetch_file(client, repo_state.repo, "CLAUDE.md")
                if content:
                    sections.append(
                        f"--- CLAUDE.md @ {repo_state.repo}\n{content}\n"
                    )
            if budget.get("readmes", 0):
                content = _fetch_file(client, repo_state.repo, "README.md")
                if content:
                    sections.append(
                        f"--- README.md @ {repo_state.repo}\n{content}\n"
                    )

        # Recent commits (heavy only)
        if budget.get("commits", 0):
            for repo_state in snapshot.repos:
                commits = _fetch_recent_commits(
                    client, repo_state.repo, n=budget["commits"]
                )
                if commits:
                    lines = [
                        f"  {c['date'][:10]} {c['sha']} {c['author']}: {c['msg']}"
                        for c in commits
                    ]
                    sections.append(
                        f"--- RECENT COMMITS @ {repo_state.repo}\n"
                        + "\n".join(lines) + "\n"
                    )

    elapsed_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    body = "\n".join(sections)
    bytes_total = len(body.encode("utf-8"))

    _log(
        "deep_read.gathered",
        level=level,
        sections=len(sections),
        bytes=bytes_total,
        elapsed_ms=elapsed_ms,
        prs=len(prs) if "prs" in locals() else 0,
        issues=len(issues) if "issues" in locals() else 0,
    )

    if not body:
        return ""
    return (
        "===== REPO EVIDENCE (deep_read=" + level + ") =====\n"
        + body
        + "===== END REPO EVIDENCE =====\n\n"
    )
