"""Tests for director.policy — scoping + auto-merge gates."""

from __future__ import annotations

from director import policy
from director.ideate import NextMove
from director.perceive import Issue, PullRequest, RepoState, Snapshot

REPO = "lkmotto/motto-social-agent"
SELF_REPO = "lkmotto/motto-director"


def _pr(
    *,
    number: int = 1,
    title: str = "PR title",
    labels: list[str] | None = None,
    ci_status: str = "success",
    approvals: int = 1,
    repo: str = REPO,
) -> PullRequest:
    return PullRequest(
        repo=repo,
        number=number,
        title=title,
        url=f"https://github.com/{repo}/pull/{number}",
        age_hours=1.0,
        ci_status=ci_status,
        review_state="approved" if approvals else "pending",
        approvals=approvals,
        labels=labels or [],
        head_sha="abc1234",
    )


def _issue(
    *,
    number: int = 10,
    title: str = "Issue title",
    labels: list[str] | None = None,
    repo: str = REPO,
) -> Issue:
    return Issue(
        repo=repo,
        number=number,
        title=title,
        url=f"https://github.com/{repo}/issues/{number}",
        age_hours=1.0,
        labels=labels or [],
    )


def _snapshot(
    *,
    prs: list[PullRequest] | None = None,
    issues: list[Issue] | None = None,
) -> Snapshot:
    state = RepoState(
        repo=REPO,
        open_prs=prs or [],
        open_issues=issues or [],
    )
    return Snapshot(captured_at="2026-05-04T00:00:00Z", repos=[state])


# ---------- spawn eligibility ----------


def test_spawn_eligible_with_director_ok_label():
    issue = _issue(labels=["director-ok", "bug"])
    ok, reason = policy.is_eligible_for_spawn(issue)
    assert ok is True
    assert "director-ok" in reason


def test_spawn_ineligible_without_label():
    issue = _issue(labels=["bug"])
    ok, reason = policy.is_eligible_for_spawn(issue)
    assert ok is False
    assert "director-ok" in reason


# ---------- tier-1 path matching ----------


def test_tier1_patterns_match_docs_lockfiles_workflows_tests():
    matchers = [
        "README.md",
        "docs/cadence.md",
        ".github/workflows/ci.yml",
        "package-lock.json",
        "poetry.lock",
        "requirements-dev.txt",
        "test_foo.py",
        "foo_test.py",
        "tests/test_x.py",
        "src/tests/test_inner.py",
    ]
    for path in matchers:
        assert policy._path_matches_tier1(path), f"{path!r} should be tier-1"


def test_non_tier1_paths_rejected():
    for path in ["src/app.py", "director/main.py", "config/settings.yaml"]:
        assert not policy._path_matches_tier1(path), f"{path!r} should NOT be tier-1"


# ---------- auto-merge ----------


def test_auto_merge_happy_with_label():
    pr = _pr(labels=["director-ok"], ci_status="success", approvals=1)
    ok, reason = policy.is_eligible_for_auto_merge(pr, "success")
    assert ok is True


def test_auto_merge_happy_with_tier1_paths_no_label():
    pr = _pr(labels=[], ci_status="success", approvals=1)
    ok, _ = policy.is_eligible_for_auto_merge(
        pr, "success", changed_paths=["README.md", "docs/foo.md"]
    )
    assert ok is True


def test_auto_merge_blocked_when_ci_failing():
    pr = _pr(labels=["director-ok"], ci_status="failure", approvals=1)
    ok, reason = policy.is_eligible_for_auto_merge(pr, "failure")
    assert ok is False
    assert "ci" in reason


def test_auto_merge_allows_ci_bot_without_review():
    pr = _pr(labels=["director-ok"], ci_status="success", approvals=0)
    ok, reason = policy.is_eligible_for_auto_merge(
        pr, "success", author="github-actions[bot]"
    )
    assert ok is True


def test_auto_merge_blocks_when_no_review_and_human_author():
    pr = _pr(labels=["director-ok"], ci_status="success", approvals=0)
    ok, reason = policy.is_eligible_for_auto_merge(pr, "success", author="some-human")
    assert ok is False
    assert "review" in reason.lower()


def test_auto_merge_blocks_self_mod_even_with_label(monkeypatch):
    monkeypatch.delenv("DIRECTOR_ALLOW_SELF_MOD", raising=False)
    pr = _pr(repo=SELF_REPO, labels=["director-ok"], approvals=1)
    ok, reason = policy.is_eligible_for_auto_merge(
        pr, "success", changed_paths=["director/policy.py"]
    )
    assert ok is False
    assert "self-mod" in reason.lower() or "DIRECTOR_ALLOW_SELF_MOD" in reason


def test_auto_merge_self_mod_passes_with_opt_in(monkeypatch):
    monkeypatch.setenv("DIRECTOR_ALLOW_SELF_MOD", "true")
    pr = _pr(repo=SELF_REPO, labels=["director-ok"], approvals=1)
    ok, _ = policy.is_eligible_for_auto_merge(
        pr, "success", changed_paths=["director/policy.py"]
    )
    assert ok is True


def test_auto_merge_blocks_oversized_diff():
    pr = _pr(labels=["director-ok"], approvals=1)
    ok, reason = policy.is_eligible_for_auto_merge(
        pr, "success", changed_loc=policy.MAX_LOC_PER_SESSION + 1
    )
    assert ok is False
    assert "diff" in reason


# ---------- estimate_session_diff_size ----------


def test_estimate_size_zero_for_empty_prompt():
    assert policy.estimate_session_diff_size("") == 0


def test_estimate_size_counts_files_and_broad_keywords():
    prompt = "Refactor src/foo.py and tests/bar.py to rewrite all of the legacy paths"
    size = policy.estimate_session_diff_size(prompt)
    # 2 file-like tokens + "refactor"*2 + "rewrite"*2 + "all of"*2 = 8
    assert size > policy.MAX_FILES_PER_SESSION


# ---------- filter_moves ----------


def _move_spawn(prompt: str = "fix bug in foo.py", title: str = "Issue title") -> NextMove:
    return NextMove(
        repo=REPO,
        kind="spawn_session",
        title=title,
        rationale="r",
        prompt_for_claude_code=prompt,
        priority=2,
        intent="i",
    )


def test_filter_moves_drops_unlabeled_spawn_session(capsys):
    issue = _issue(title="Issue title", labels=["bug"])
    snap = _snapshot(issues=[issue])
    moves = [_move_spawn()]
    kept = policy.filter_moves(moves, snap)
    assert kept == []
    out = capsys.readouterr().out
    assert "policy.decision" in out


def test_filter_moves_keeps_labeled_spawn_session():
    issue = _issue(title="Issue title", labels=["director-ok"])
    snap = _snapshot(issues=[issue])
    moves = [_move_spawn(prompt="tweak retry timeout")]
    kept = policy.filter_moves(moves, snap)
    assert len(kept) == 1


def test_filter_moves_manual_mode_keeps_unlabeled_spawn_session():
    """Manual mode bypasses the director-ok label gate; human reviews queue."""
    issue = _issue(title="Issue title", labels=["bug"])
    snap = _snapshot(issues=[issue])
    moves = [_move_spawn(prompt="tweak retry timeout")]
    kept = policy.filter_moves(moves, snap, manual_mode=True)
    assert len(kept) == 1


def test_filter_moves_manual_mode_still_drops_oversized_prompt():
    """Manual mode still enforces prompt-scope safeguard (not a human-judgment gate)."""
    issue = _issue(title="Issue title", labels=["bug"])
    snap = _snapshot(issues=[issue])
    moves = [
        _move_spawn(
            prompt=(
                "rewrite src/a.py src/b.py src/c.py src/d.py and refactor "
                "all of the helpers"
            )
        )
    ]
    kept = policy.filter_moves(moves, snap, manual_mode=True)
    assert kept == []


def test_filter_moves_drops_oversized_prompt_even_when_labeled():
    issue = _issue(title="Issue title", labels=["director-ok"])
    snap = _snapshot(issues=[issue])
    moves = [
        _move_spawn(
            prompt=(
                "rewrite src/a.py src/b.py src/c.py src/d.py and refactor "
                "all of the helpers"
            )
        )
    ]
    kept = policy.filter_moves(moves, snap)
    assert kept == []


def test_filter_moves_drops_compound_pr_self_mod_without_optin(monkeypatch):
    monkeypatch.delenv("DIRECTOR_ALLOW_SELF_MOD", raising=False)
    move = NextMove(
        repo=SELF_REPO,
        kind="compound_pr",
        title="self mod",
        rationale="r",
        prompt_for_claude_code="",
        priority=3,
        intent="i",
        code_changes=[{"path": "director/policy.py", "content": "x"}],
    )
    kept = policy.filter_moves([move], _snapshot())
    assert kept == []


def test_filter_moves_passes_through_file_issue():
    move = NextMove(
        repo=REPO,
        kind="file_issue",
        title="t",
        rationale="r",
        prompt_for_claude_code="",
        priority=3,
        intent="i",
    )
    kept = policy.filter_moves([move], _snapshot())
    assert kept == [move]


def test_filter_moves_drops_disabled_kinds(monkeypatch):
    """DIRECTOR_DISABLED_KINDS=merge_pr should block merges even if they
    would otherwise be eligible. Used for first non-DRY_RUN tick safety."""
    monkeypatch.setenv("DIRECTOR_DISABLED_KINDS", "merge_pr,spawn_session")
    file_issue = NextMove(
        repo=REPO,
        kind="file_issue",
        title="file me",
        rationale="r",
        prompt_for_claude_code="",
        priority=3,
        intent="i",
    )
    merge = NextMove(
        repo=REPO,
        kind="merge_pr",
        title="merge me",
        rationale="r",
        prompt_for_claude_code="",
        priority=3,
        intent="i",
    )
    spawn = NextMove(
        repo=REPO,
        kind="spawn_session",
        title="spawn me",
        rationale="r",
        prompt_for_claude_code="do thing",
        priority=3,
        intent="i",
    )
    kept = policy.filter_moves([file_issue, merge, spawn], _snapshot())
    # merge_pr and spawn_session dropped by env gate; file_issue passes through.
    assert [m.kind for m in kept] == ["file_issue"]
