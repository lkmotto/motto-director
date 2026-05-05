"""Scoping policy for director moves: what's eligible to spawn / auto-merge.

The director sees more candidate work each cycle than it can safely run.
This module enforces two gates:

1. spawn_session — only the labelled allowlist (`director-ok`) or PRs/issues
   whose changed paths are all "tier-1" (docs, lockfile bumps, CI version
   pins, tests). Everything else is filtered out before it can spend a
   Claude Code session.
2. merge_pr — auto-merge only when CI is green, an approving review exists
   (or the author is the trusted CI bot), the change is tier-1 OR carries
   the `director-ok` label, the diff is small, AND the change is not a
   self-mod (unless DIRECTOR_ALLOW_SELF_MOD=1 is set).

Self-mod is the one rule that label cannot bypass.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from director.compound import allow_self_mod, is_self_mod
from director.ideate import NextMove
from director.perceive import Issue, PullRequest, Snapshot

AUTO_TASK_LABEL = "director-ok"

# Filename glob patterns that are safe enough to ship without an explicit
# allowlist label. Anything matching one of these is considered "tier-1".
TIER_1_PATTERNS: tuple[str, ...] = (
    "*.md",
    "docs/**",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    "package-lock.json",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "requirements*.txt",
    "test_*.py",
    "*_test.py",
    "tests/**",
    "**/tests/**",
)

MAX_FILES_PER_SESSION = 3
MAX_LOC_PER_SESSION = 200

CI_BOT_AUTHOR = "github-actions[bot]"


@dataclass
class Decision:
    """Recorded reason a move was kept or dropped — emitted to fleet."""

    move_kind: str
    repo: str
    title: str
    eligible: bool
    reason: str


def _log(event_name: str, **fields: object) -> None:
    record = {"ts": datetime.now(UTC).isoformat(), "event": event_name, **fields}
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


def _path_matches_tier1(path: str) -> bool:
    norm = path.lstrip("/")
    for pattern in TIER_1_PATTERNS:
        if fnmatch.fnmatch(norm, pattern):
            return True
        # fnmatch doesn't treat ** as recursive; emulate it by stripping
        # the trailing /** and checking prefix.
        if pattern.endswith("/**"):
            prefix = pattern[:-3]
            if norm == prefix or norm.startswith(prefix + "/"):
                return True
        if pattern.startswith("**/"):
            suffix = pattern[3:]
            if fnmatch.fnmatch(norm, suffix) or norm.endswith("/" + suffix):
                return True
    return False


def _all_paths_tier1(paths: list[str]) -> bool:
    return bool(paths) and all(_path_matches_tier1(p) for p in paths)


def _labels(target: Issue | PullRequest) -> list[str]:
    return list(getattr(target, "labels", []) or [])


def is_eligible_for_spawn(target: Issue | PullRequest) -> tuple[bool, str]:
    """Return (eligible, reason). Allowlist label OR all-tier-1 file paths.

    Issues don't carry diff paths, so for issues the only signal is the
    allowlist label.
    """
    labels = _labels(target)
    if AUTO_TASK_LABEL in labels:
        return True, f"label:{AUTO_TASK_LABEL}"

    # PRs may carry path info via `changed_files` if the snapshot ever grows
    # one. For now we don't have that, so issues/PRs without the label fail
    # the gate — the spawn_session prompt itself is the next line of defense
    # via estimate_session_diff_size.
    return False, f"missing label:{AUTO_TASK_LABEL}"


def is_eligible_for_auto_merge(
    pr: PullRequest, ci_state: str, *, changed_paths: list[str] | None = None,
    changed_loc: int | None = None, author: str | None = None,
) -> tuple[bool, str]:
    """Auto-merge gate. See module docstring for the full rule set."""
    if ci_state != "success":
        return False, f"ci:{ci_state}"

    has_review = pr.approvals >= 1
    is_ci_bot = (author or "") == CI_BOT_AUTHOR
    if not has_review and not is_ci_bot:
        return False, "no approving review and author != ci bot"

    labels = _labels(pr)
    paths = changed_paths or []
    label_ok = AUTO_TASK_LABEL in labels
    tier1_ok = _all_paths_tier1(paths) if paths else False
    if not (label_ok or tier1_ok):
        return False, f"missing label:{AUTO_TASK_LABEL} and not all tier-1"

    if is_self_mod(pr.repo, paths) and not allow_self_mod():
        return False, "self-modifying path; requires DIRECTOR_ALLOW_SELF_MOD=1"

    if changed_loc is not None and changed_loc > MAX_LOC_PER_SESSION:
        return False, f"diff:{changed_loc}>max:{MAX_LOC_PER_SESSION}"

    if label_ok:
        return True, f"label:{AUTO_TASK_LABEL}"
    return True, "tier-1 paths only"


def estimate_session_diff_size(prompt: str) -> int:
    """Rough heuristic — used to filter overly-broad spawn_session prompts.

    Counts file-shaped tokens (e.g. `foo/bar.py`, `module.ts`) plus
    function-shaped requests ("rewrite", "refactor", "all of"). The number
    is dimensionless: above MAX_FILES_PER_SESSION the prompt is too broad.
    """
    if not prompt:
        return 0
    tokens = prompt.lower().split()
    file_like = sum(
        1
        for t in tokens
        if "/" in t or t.endswith((".py", ".ts", ".tsx", ".js", ".md", ".yml", ".yaml"))
    )
    broad_keywords = ("rewrite", "refactor", "overhaul", "redesign", "all of", "every")
    broad_hits = sum(prompt.lower().count(k) for k in broad_keywords)
    return file_like + broad_hits * 2


def _find_pr(snapshot: Snapshot, repo: str, title: str) -> PullRequest | None:
    for state in snapshot.repos:
        if state.repo != repo:
            continue
        for pr in state.open_prs:
            if pr.title == title or str(pr.number) in title:
                return pr
    return None


def _find_issue(snapshot: Snapshot, repo: str, title: str) -> Issue | None:
    for state in snapshot.repos:
        if state.repo != repo:
            continue
        for issue in state.open_issues:
            if issue.title == title or str(issue.number) in title:
                return issue
    return None


def _disabled_kinds() -> set[str]:
    """Return the set of move kinds disabled by env (operator override).

    Set DIRECTOR_DISABLED_KINDS to a comma-separated list of MoveKind values
    to drop them before policy evaluation. Useful when graduating from
    DRY_RUN: e.g. DIRECTOR_DISABLED_KINDS=merge_pr,spawn_session lets the
    director file issues + draft compound PRs but blocks merges and Claude
    Code session spawns.
    """
    raw = os.environ.get("DIRECTOR_DISABLED_KINDS", "").strip()
    if not raw:
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


def filter_moves(moves: list[NextMove], snapshot: Snapshot) -> list[NextMove]:
    """Apply scoping rules. Drop moves that fail eligibility; emit a
    `policy.decision` log line per drop with rationale."""
    disabled = _disabled_kinds()
    kept: list[NextMove] = []
    for move in moves:
        if move.kind in disabled:
            _log(
                "policy.decision",
                kind=move.kind,
                repo=move.repo,
                title=move.title,
                eligible=False,
                reason="kind disabled by DIRECTOR_DISABLED_KINDS env",
            )
            continue
        eligible, reason = _evaluate(move, snapshot)
        if eligible:
            kept.append(move)
            continue
        _log(
            "policy.decision",
            kind=move.kind,
            repo=move.repo,
            title=move.title,
            eligible=False,
            reason=reason,
        )
    return kept


def _evaluate(move: NextMove, snapshot: Snapshot) -> tuple[bool, str]:
    if move.kind == "spawn_session":
        target: Issue | PullRequest | None = _find_issue(
            snapshot, move.repo, move.title
        ) or _find_pr(snapshot, move.repo, move.title)
        if target is not None:
            ok, reason = is_eligible_for_spawn(target)
            if not ok:
                return False, reason
        # Even with a labelled target (or no resolvable target), reject
        # prompts that look like multi-file refactors — those are the
        # bandwidth wasters this whole module exists to stop.
        size = estimate_session_diff_size(move.prompt_for_claude_code)
        if size > MAX_FILES_PER_SESSION:
            return False, f"prompt scope estimate {size} > {MAX_FILES_PER_SESSION}"
        return True, "ok"

    if move.kind == "merge_pr":
        pr = _find_pr(snapshot, move.repo, move.title)
        if pr is None:
            return False, "PR not found in snapshot"
        return is_eligible_for_auto_merge(pr, pr.ci_status)

    if move.kind == "compound_pr":
        paths = [c.get("path", "") for c in move.code_changes]
        if is_self_mod(move.repo, paths) and not allow_self_mod():
            return False, "self-modifying path; requires DIRECTOR_ALLOW_SELF_MOD=1"
        return True, "ok"

    # file_issue, nudge_pipeline, noop — no policy gate.
    return True, "ok"


def director_max_concurrent_default() -> int:
    """Read DIRECTOR_MAX_CONCURRENT_SESSIONS or default 3. Exported so
    concurrency.py can share one parsing implementation."""
    raw = os.environ.get("DIRECTOR_MAX_CONCURRENT_SESSIONS")
    if raw is None:
        return 3
    try:
        return max(1, int(raw))
    except ValueError:
        return 3
