"""GitHub PR-outcome signals: merge rate, time-to-merge, reverts, CI flake.

Same shape contract as langfuse_signals.collect() / postgres_signals.collect()
— a stable dict the synthesizer consumes even when unconfigured.

Uses ``gh`` CLI (already authenticated for the rest of the codebase via
``GITHUB_TOKEN`` / ``GH_TOKEN``) so we inherit the existing auth path
instead of inventing a new one.  Pure subprocess; no extra deps.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Repos the flywheel inspects when computing fleet-wide PR signals.  Falls
# back to the env override used elsewhere in director.
_DEFAULT_REPOS = [
    "lkmotto/motto-director",
    "lkmotto/motto-mcp-server",
    "lkmotto/motto-credential-grabber",
    "lkmotto/motto-sdr-agent",
    "lkmotto/motto-appraisal-pipeline",
]


def _watch_repos() -> list[str]:
    raw = os.environ.get("WATCH_REPOS") or ""
    repos = [r.strip() for r in raw.split(",") if r.strip()]
    return repos or _DEFAULT_REPOS


def is_configured() -> bool:
    """gh CLI must be on PATH and a token must be visible to it."""
    if not shutil.which("gh"):
        return False
    return bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_PAT"))


def _empty() -> dict[str, Any]:
    return {
        "configured": False,
        "repo_count": 0,
        "merge_rate": 0.0,
        "avg_time_to_merge_hours": 0.0,
        "auto_merge_ok_count": 0,
        "reverted_pr_numbers": [],
        "ci_flake_repos": [],
        "per_repo": {},
    }


def collect(since_days: int = 7) -> dict[str, Any]:
    """Return PR-outcome signals across the watch repos.

    Synchronous (gh is subprocess-based).  Caller can schedule via
    ``asyncio.to_thread`` if it wants concurrency with the other
    collectors.  Returns ``_empty()`` if gh isn't configured.
    """
    if not is_configured():
        logger.info("github_signals: gh not configured, returning empty")
        return _empty()

    repos = _watch_repos()
    cutoff = datetime.now(UTC) - timedelta(days=since_days)

    total_prs = 0
    merged_prs = 0
    ttm_hours: list[float] = []
    auto_merge_ok = 0
    reverted: list[dict[str, Any]] = []
    ci_flake_repos: list[str] = []
    per_repo: dict[str, dict[str, Any]] = {}

    for repo in repos:
        snapshot = _repo_snapshot(repo, since_days)
        if snapshot is None:
            continue
        per_repo[repo] = snapshot
        total_prs += snapshot["pr_count"]
        merged_prs += snapshot["merged_count"]
        ttm_hours.extend(snapshot["ttm_hours"])
        auto_merge_ok += snapshot["auto_merge_ok_count"]

        for pr in snapshot["reverts"]:
            reverted.append({"repo": repo, **pr})

        if snapshot.get("ci_flake_score", 0.0) >= 0.25:  # noqa: PLR2004
            ci_flake_repos.append(repo)

    merge_rate = (merged_prs / total_prs) if total_prs else 0.0
    avg_ttm = (sum(ttm_hours) / len(ttm_hours)) if ttm_hours else 0.0

    logger.info(
        "github_signals: repos=%d prs=%d merged=%d merge_rate=%.2f "
        "avg_ttm_h=%.1f reverts=%d flake_repos=%d",
        len(per_repo),
        total_prs,
        merged_prs,
        merge_rate,
        avg_ttm,
        len(reverted),
        len(ci_flake_repos),
    )

    return {
        "configured": True,
        "repo_count": len(per_repo),
        "merge_rate": merge_rate,
        "avg_time_to_merge_hours": avg_ttm,
        "auto_merge_ok_count": auto_merge_ok,
        "reverted_pr_numbers": reverted,
        "ci_flake_repos": ci_flake_repos,
        "per_repo": per_repo,
        "since_iso": cutoff.isoformat(),
    }


# ---------------------------------------------------------------------------
# Per-repo helpers
# ---------------------------------------------------------------------------


def _gh(args: list[str]) -> tuple[int, str, str]:
    """Run gh with stdout+stderr captured. Returns (rc, stdout, stderr)."""
    try:
        cp = subprocess.run(  # noqa: S603
            ["gh", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("gh subprocess failed: %s", exc)
        return 1, "", str(exc)
    return cp.returncode, cp.stdout, cp.stderr


def _repo_snapshot(repo: str, since_days: int) -> dict[str, Any] | None:
    """Per-repo PR snapshot, or None on persistent gh errors."""
    rc, stdout, _ = _gh(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--limit",
            "100",
            "--json",
            "number,title,state,mergedAt,createdAt,labels,headRefName",
        ]
    )
    if rc != 0:
        return None
    try:
        prs: list[dict[str, Any]] = json.loads(stdout) or []
    except json.JSONDecodeError:
        return None

    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    recent = [p for p in prs if _parse_iso(p.get("createdAt")) >= cutoff]

    pr_count = len(recent)
    merged = [p for p in recent if p.get("mergedAt")]
    merged_count = len(merged)
    ttm_hours = [
        max(0.0, (_parse_iso(p["mergedAt"]) - _parse_iso(p["createdAt"])).total_seconds() / 3600.0)
        for p in merged
    ]
    auto_merge_ok_count = sum(
        1
        for p in recent
        if any(
            lab.get("name") == "auto-merge-ok"
            for lab in p.get("labels") or []
        )
    )
    reverts = _detect_reverts(prs)
    ci_flake_score = _ci_flake_score(repo, recent)

    return {
        "pr_count": pr_count,
        "merged_count": merged_count,
        "ttm_hours": ttm_hours,
        "auto_merge_ok_count": auto_merge_ok_count,
        "reverts": reverts,
        "ci_flake_score": ci_flake_score,
    }


def _detect_reverts(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find merged PRs whose titles look like reverts of another PR.

    Heuristic: title starts with 'Revert' or contains 'revert #N'. We
    surface the originating PR number when parseable.
    """
    out: list[dict[str, Any]] = []
    for p in prs:
        if not p.get("mergedAt"):
            continue
        title = (p.get("title") or "").strip()
        lower = title.lower()
        if lower.startswith("revert ") or "revert #" in lower or 'revert "' in lower:
            out.append({"number": p.get("number"), "title": title})
    return out


def _ci_flake_score(repo: str, prs: list[dict[str, Any]]) -> float:
    """Cheap proxy for flakiness: fraction of merged PRs whose default
    branch CI run had >=2 distinct conclusions in the last 30 runs."""
    rc, stdout, _ = _gh(
        [
            "run",
            "list",
            "--repo",
            repo,
            "--branch",
            "main",
            "--limit",
            "30",
            "--json",
            "conclusion",
        ]
    )
    if rc != 0:
        return 0.0
    try:
        runs: list[dict[str, Any]] = json.loads(stdout) or []
    except json.JSONDecodeError:
        return 0.0
    if not runs:
        return 0.0
    conclusions = Counter(r.get("conclusion") or "unknown" for r in runs)
    successes = conclusions.get("success", 0)
    total = sum(conclusions.values())
    return 1.0 - (successes / total) if total else 0.0


def _parse_iso(s: str | None) -> datetime:
    if not s:
        return datetime.min.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=UTC)
