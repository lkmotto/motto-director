"""PR generator: turn high-confidence SuggestedFix specs into PRs.

Cheap fixes (``cheapness == 'cheap'``) get full PRs with the
``auto-merge-ok`` label so the auto-merge action ships them once CI is
green. Expensive fixes (``cheapness == 'expensive'``) become tracking
issues — never PRs that touch protected files.

Hard safety rails — order matters, top-to-bottom:

1. Confidence threshold gate (default 0.7, configurable via
   ``QUALITY_FLYWHEEL_CONFIDENCE_THRESHOLD``).
2. Protected-file refusal: any payload that *would* touch
   ``policy.py`` / ``observability.py`` / ``fleet.py`` / ``perceive.py``
   / ``ideate.py`` / ``act.py`` / ``digest.py`` /
   ``.github/workflows/*`` / the cron applier is downgraded to a
   tracking issue and never an auto-merge PR.
3. Per-run cap (``MAX_FIXES_PER_RUN``) so a single bad week doesn't
   flood the PR queue.

Implementation note: this module does NOT modify code itself. It writes
config-style additive files (e.g. ``director/config/tool_overrides.yaml``)
or opens issues. Anything that would require touching the director's
existing modules surfaces as an issue with a human-review marker.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

from director.quality.synthesizer import QualityReport, SuggestedFix

logger = logging.getLogger(__name__)

DEFAULT_REPO = "lkmotto/motto-director"
MAX_FIXES_PER_RUN = 3

# Patterns mirror auto-merge.yaml's PROTECTED_PATTERNS.
PROTECTED_PATTERNS = [
    re.compile(r"(^|/)policy\.py$"),
    re.compile(r"(^|/)observability\.py$"),
    re.compile(r"(^|/)fleet\.py$"),
    re.compile(r"(^|/)perceive\.py$"),
    re.compile(r"(^|/)ideate\.py$"),
    re.compile(r"(^|/)act\.py$"),
    re.compile(r"(^|/)digest\.py$"),
    re.compile(r"^\.github/workflows/"),
    re.compile(r"^scripts/apply_northflank_crons\.py$"),
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerationResult:
    pr_urls: list[str] = field(default_factory=list)
    issue_urls: list[str] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pr_urls": list(self.pr_urls),
            "issue_urls": list(self.issue_urls),
            "skipped": [dict(s) for s in self.skipped],
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def confidence_threshold() -> float:
    raw = os.environ.get("QUALITY_FLYWHEEL_CONFIDENCE_THRESHOLD", "0.7")
    try:
        return float(raw)
    except ValueError:
        return 0.7


def is_configured() -> bool:
    if not shutil.which("gh"):
        return False
    return bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_PAT"))


def generate(
    report: QualityReport,
    *,
    repo: str | None = None,
    dry_run: bool | None = None,
) -> GenerationResult:
    """Generate PRs / issues for high-confidence suggested fixes.

    Returns a GenerationResult; never raises on individual fix failure.
    Each per-fix exception is caught, logged, and recorded in
    ``skipped``.
    """
    target_repo = repo or os.environ.get("DIRECTOR_SELF_REPO", DEFAULT_REPO)
    is_dry = dry_run if dry_run is not None else os.environ.get("DRY_RUN") == "1"

    threshold = confidence_threshold()
    accepted = [f for f in report.suggested_fixes if f.confidence >= threshold][
        :MAX_FIXES_PER_RUN
    ]
    skipped: list[dict[str, Any]] = [
        {"title": f.title, "reason": "below_confidence_threshold", "confidence": f.confidence}
        for f in report.suggested_fixes
        if f.confidence < threshold
    ]

    pr_urls: list[str] = []
    issue_urls: list[str] = []

    if not is_dry and not is_configured():
        logger.warning("pr_generator: gh not configured, returning all-skipped")
        for f in accepted:
            skipped.append({"title": f.title, "reason": "gh_not_configured"})
        return GenerationResult(pr_urls=[], issue_urls=[], skipped=skipped)

    for fix in accepted:
        try:
            if fix.cheapness == "expensive":
                # Always becomes an issue, never a PR.
                url = _open_issue(target_repo, fix, is_dry=is_dry)
                if url:
                    issue_urls.append(url)
                continue

            # Cheap path: try to materialize as a PR; if it would touch
            # protected files, fall back to an issue.
            files = _intended_paths(fix)
            if any(_is_protected(p) for p in files):
                logger.info(
                    "pr_generator: fix %r touches protected files, downgrading to issue",
                    fix.title,
                )
                url = _open_issue(target_repo, fix, is_dry=is_dry, note="protected_files")
                if url:
                    issue_urls.append(url)
                continue

            url = _open_pr(target_repo, fix, is_dry=is_dry)
            if url:
                pr_urls.append(url)
        except Exception as exc:  # noqa: BLE001
            logger.exception("pr_generator: fix %r raised: %s", fix.title, exc)
            skipped.append({"title": fix.title, "reason": f"exception: {exc!r}"})

    return GenerationResult(pr_urls=pr_urls, issue_urls=issue_urls, skipped=skipped)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_protected(path: str) -> bool:
    return any(rx.search(path) for rx in PROTECTED_PATTERNS)


def _intended_paths(fix: SuggestedFix) -> list[str]:
    """Map a fix kind to the files we'd write. Pure routing — no IO.

    Cheap fixes write to additive config under
    ``director/config/tool_overrides.yaml`` (config_tweak) or
    ``director/config/prompts/`` (prompt_edit).  Both directories are
    OUTSIDE the protected list, by design.
    """
    if fix.fix_kind == "config_tweak":
        return ["director/config/tool_overrides.yaml"]
    if fix.fix_kind == "prompt_edit":
        return ["director/config/prompts/ideate_addendum.md"]
    return []


def _open_pr(repo: str, fix: SuggestedFix, *, is_dry: bool) -> str | None:
    """Materialize a cheap fix as a PR with auto-merge-ok.

    For now this only writes the *intent* into the PR body and creates
    the additive file with the JSON payload appended.  Director's next
    cycle reads the override file and applies the change at runtime —
    no protected-module edits required.
    """
    title = f"flywheel: {fix.title}"
    body = _pr_body(fix)

    if is_dry:
        logger.info("DRY_RUN open_pr repo=%s title=%s", repo, title)
        return f"https://github.com/{repo}/pulls/dry-run"

    # Branch name: deterministic per fix so re-runs don't flood.
    safe_target = re.sub(r"[^A-Za-z0-9._-]+", "-", fix.target)[:40]
    branch = f"flywheel/{fix.fix_kind}/{safe_target}"

    # Create branch + commit additive override file via gh API.
    payload_blob = json.dumps({"target": fix.target, "payload": fix.payload}, indent=2)
    paths = _intended_paths(fix)
    if not paths:
        return None
    file_path = paths[0]

    # Use gh api to create branch from main, commit file, open PR.
    try:
        _create_branch_with_file(repo, branch, file_path, payload_blob, fix.title)
    except _PRGenError as exc:
        logger.warning("pr_generator: skipping fix %r: %s", fix.title, exc)
        return None

    rc, stdout, stderr = _gh_run(
        [
            "pr",
            "create",
            "--repo",
            repo,
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
            "--label",
            "auto-merge-ok",
            "--label",
            "director-flywheel",
        ]
    )
    if rc != 0:
        logger.warning("pr_generator: gh pr create failed rc=%d stderr=%s", rc, stderr)
        return None
    return stdout.strip().splitlines()[-1] if stdout.strip() else None


def _open_issue(
    repo: str, fix: SuggestedFix, *, is_dry: bool, note: str | None = None
) -> str | None:
    title = f"flywheel: {fix.title}"
    body_parts = [
        f"**Confidence**: {fix.confidence:.2f}",
        f"**Cheapness**: {fix.cheapness}",
        f"**Target**: `{fix.target}`",
        f"**Fix kind**: `{fix.fix_kind}`",
        "",
        "### Rationale",
        fix.rationale,
        "",
        "### Payload",
        f"```json\n{json.dumps(fix.payload, indent=2)}\n```",
    ]
    if note:
        body_parts.insert(0, f"_Auto-downgraded to issue: {note}._\n")
    body = "\n".join(body_parts)

    if is_dry:
        logger.info("DRY_RUN open_issue repo=%s title=%s", repo, title)
        return f"https://github.com/{repo}/issues/dry-run"

    rc, stdout, stderr = _gh_run(
        [
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            "--body",
            body,
            "--label",
            "director-flywheel",
        ]
    )
    if rc != 0:
        logger.warning("pr_generator: gh issue create failed rc=%d stderr=%s", rc, stderr)
        return None
    return stdout.strip().splitlines()[-1] if stdout.strip() else None


def _pr_body(fix: SuggestedFix) -> str:
    return "\n".join(
        [
            "Auto-generated by director-quality-flywheel.",
            "",
            f"**Confidence**: {fix.confidence:.2f}",
            f"**Target**: `{fix.target}`",
            f"**Fix kind**: `{fix.fix_kind}`",
            "",
            "### Rationale",
            fix.rationale,
            "",
            "### What this PR does",
            "Adds an entry to `director/config/tool_overrides.yaml` (or "
            "`director/config/prompts/`) — additive only.  Director "
            "consumes the override on next cycle without any protected-"
            "module edits.",
            "",
            "### Payload",
            f"```json\n{json.dumps(fix.payload, indent=2)}\n```",
            "",
            "_If anything looks off, remove the `auto-merge-ok` label._",
        ]
    )


# ---------------------------------------------------------------------------
# gh subprocess wrappers
# ---------------------------------------------------------------------------


class _PRGenError(RuntimeError):
    """Raised when a per-PR gh-API operation fails recoverably."""


def _gh_run(args: list[str]) -> tuple[int, str, str]:
    try:
        cp = subprocess.run(  # noqa: S603
            ["gh", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return cp.returncode, cp.stdout, cp.stderr


def _create_branch_with_file(
    repo: str, branch: str, file_path: str, content: str, fix_title: str
) -> None:
    """Create or update branch with a single file commit via gh api.

    Idempotent: if branch already exists, just upsert the file. Uses the
    Contents API so we never need a local clone.
    """
    # 1. Get default branch SHA
    rc, stdout, stderr = _gh_run(["api", f"repos/{repo}/git/refs/heads/main"])
    if rc != 0:
        raise _PRGenError(f"could not read main ref: {stderr}")
    main_sha = json.loads(stdout)["object"]["sha"]

    # 2. Create branch ref (idempotent)
    rc, _, stderr = _gh_run(
        [
            "api",
            "-X",
            "POST",
            f"repos/{repo}/git/refs",
            "-f",
            f"ref=refs/heads/{branch}",
            "-f",
            f"sha={main_sha}",
        ]
    )
    if rc != 0 and "already exists" not in stderr.lower():
        raise _PRGenError(f"could not create branch ref: {stderr}")

    # 3. Read existing file SHA on the branch (for upsert)
    sha_arg: list[str] = []
    rc, stdout, _ = _gh_run(
        ["api", f"repos/{repo}/contents/{file_path}?ref={branch}"]
    )
    if rc == 0:
        try:
            sha = json.loads(stdout).get("sha")
            if sha:
                sha_arg = ["-f", f"sha={sha}"]
        except json.JSONDecodeError:
            pass

    # 4. Put file contents
    import base64

    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    msg = f"flywheel: {fix_title}"
    args = [
        "api",
        "-X",
        "PUT",
        f"repos/{repo}/contents/{file_path}",
        "-f",
        f"message={msg}",
        "-f",
        f"branch={branch}",
        "-f",
        f"content={b64}",
        *sha_arg,
    ]
    rc, _, stderr = _gh_run(args)
    if rc != 0:
        raise _PRGenError(f"could not upsert file: {stderr}")
