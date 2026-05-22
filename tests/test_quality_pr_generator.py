"""Tests for director.quality.pr_generator — protected-file refusal,
threshold gate, cheap vs expensive routing."""

from __future__ import annotations

from director.quality import pr_generator
from director.quality.pr_generator import (
    MAX_FIXES_PER_RUN,
    PROTECTED_PATTERNS,
    GenerationResult,
    _intended_paths,
    _is_protected,
    confidence_threshold,
    generate,
)
from director.quality.synthesizer import QualityReport, SuggestedFix

# ── pure helpers ────────────────────────────────────────────────────────────


def test_protected_patterns_match_director_modules():
    for path in (
        "director/policy.py",
        "director/observability.py",
        "director/fleet.py",
        "director/perceive.py",
        "director/ideate.py",
        "director/act.py",
        "director/digest.py",
        ".github/workflows/auto-merge.yaml",
        "scripts/apply_northflank_crons.py",
    ):
        assert _is_protected(path), f"{path!r} should be protected"


def test_protected_patterns_dont_match_safe_paths():
    for path in (
        "director/config/tool_overrides.yaml",
        "director/config/prompts/ideate_addendum.md",
        "tests/test_quality_synthesizer.py",
        "README.md",
        # `.github/policy.py` would be protected, but a file named like a
        # protected module living deep in docs/ should still match — that's
        # by design (defensive). Validate the inverse here.
    ):
        assert not _is_protected(path), f"{path!r} should NOT be protected"


def test_protected_patterns_count_matches_auto_merge_action():
    # 9 patterns: 7 director modules + workflows dir + cron script.
    assert len(PROTECTED_PATTERNS) == 9


def test_intended_paths_routes_config_tweak_to_overrides_yaml():
    fix = _make_fix(fix_kind="config_tweak", cheapness="cheap")
    assert _intended_paths(fix) == ["director/config/tool_overrides.yaml"]


def test_intended_paths_routes_prompt_edit_to_prompts_dir():
    fix = _make_fix(fix_kind="prompt_edit", cheapness="cheap")
    assert _intended_paths(fix) == ["director/config/prompts/ideate_addendum.md"]


def test_intended_paths_returns_empty_for_review_fixes():
    fix = _make_fix(fix_kind="flag_for_review", cheapness="expensive")
    assert _intended_paths(fix) == []


# ── confidence threshold ────────────────────────────────────────────────────


def test_confidence_threshold_default(monkeypatch):
    monkeypatch.delenv("QUALITY_FLYWHEEL_CONFIDENCE_THRESHOLD", raising=False)
    assert confidence_threshold() == 0.7


def test_confidence_threshold_override(monkeypatch):
    monkeypatch.setenv("QUALITY_FLYWHEEL_CONFIDENCE_THRESHOLD", "0.85")
    assert confidence_threshold() == 0.85


def test_confidence_threshold_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("QUALITY_FLYWHEEL_CONFIDENCE_THRESHOLD", "not-a-float")
    assert confidence_threshold() == 0.7


# ── generate() routing ──────────────────────────────────────────────────────


def _make_fix(
    *,
    fix_kind: str = "config_tweak",
    cheapness: str = "cheap",
    confidence: float = 0.9,
    target: str = "toolA",
    title: str | None = None,
) -> SuggestedFix:
    return SuggestedFix(
        title=title or f"fix-{fix_kind}-{target}",
        rationale="r",
        confidence=confidence,
        cheapness=cheapness,
        target=target,
        fix_kind=fix_kind,
        payload={"k": "v"},
    )


def _report(fixes: list[SuggestedFix]) -> QualityReport:
    return QualityReport(
        top_problems=[f.title for f in fixes][:3],
        suggested_fixes=fixes,
        confidence_score=(sum(f.confidence for f in fixes) / len(fixes)) if fixes else 0.0,
        signals={},
    )


def test_generate_dry_run_cheap_fix_returns_pr_url():
    rep = _report([_make_fix(cheapness="cheap", confidence=0.9)])
    out = generate(rep, dry_run=True)
    assert isinstance(out, GenerationResult)
    assert len(out.pr_urls) == 1
    assert "dry-run" in out.pr_urls[0]
    assert out.issue_urls == []
    assert out.skipped == []


def test_generate_dry_run_expensive_fix_becomes_issue():
    rep = _report([_make_fix(cheapness="expensive", fix_kind="flag_for_review", confidence=0.95)])
    out = generate(rep, dry_run=True)
    assert out.pr_urls == []
    assert len(out.issue_urls) == 1
    assert "dry-run" in out.issue_urls[0]


def test_generate_below_threshold_is_skipped(monkeypatch):
    monkeypatch.setenv("QUALITY_FLYWHEEL_CONFIDENCE_THRESHOLD", "0.7")
    rep = _report(
        [
            _make_fix(cheapness="cheap", confidence=0.5, target="t1"),
            _make_fix(cheapness="cheap", confidence=0.8, target="t2"),
        ]
    )
    out = generate(rep, dry_run=True)
    assert len(out.pr_urls) == 1  # only the 0.8 one
    assert any(s.get("reason") == "below_confidence_threshold" for s in out.skipped)


def test_generate_caps_at_max_fixes_per_run():
    fixes = [
        _make_fix(cheapness="cheap", confidence=0.95, target=f"t{i}")
        for i in range(MAX_FIXES_PER_RUN + 2)
    ]
    rep = _report(fixes)
    out = generate(rep, dry_run=True)
    assert len(out.pr_urls) == MAX_FIXES_PER_RUN


def test_generate_protected_payload_is_downgraded_to_issue(monkeypatch):
    """A cheap fix whose intended_paths would touch a protected file is
    downgraded to an issue. Simulate this by monkeypatching _intended_paths
    so the fix routes to a protected file."""
    monkeypatch.setattr(
        pr_generator,
        "_intended_paths",
        lambda fix: ["director/policy.py"],
    )
    rep = _report([_make_fix(cheapness="cheap", confidence=0.95)])
    out = generate(rep, dry_run=True)
    assert out.pr_urls == []
    assert len(out.issue_urls) == 1


def test_generate_when_gh_unconfigured_marks_all_skipped(monkeypatch):
    """No DRY_RUN, no gh CLI on PATH — every accepted fix appears in
    skipped with reason gh_not_configured."""
    monkeypatch.setattr(pr_generator, "is_configured", lambda: False)
    rep = _report(
        [
            _make_fix(cheapness="cheap", confidence=0.9, target="t1"),
            _make_fix(
                cheapness="expensive", fix_kind="flag_for_review", confidence=0.9, target="t2"
            ),
        ]
    )
    out = generate(rep, dry_run=False)
    assert out.pr_urls == []
    assert out.issue_urls == []
    reasons = {s["reason"] for s in out.skipped}
    assert "gh_not_configured" in reasons


def test_generate_handles_per_fix_exception_in_real_path(monkeypatch):
    """One failing fix doesn't sink the whole batch."""
    monkeypatch.setattr(pr_generator, "is_configured", lambda: True)
    calls = {"n": 0}

    def fake_open_pr(repo, fix, *, is_dry):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return f"https://github.com/x/y/pull/{calls['n']}"

    monkeypatch.setattr(pr_generator, "_open_pr", fake_open_pr)
    rep = _report(
        [
            _make_fix(cheapness="cheap", confidence=0.9, target="t1"),
            _make_fix(cheapness="cheap", confidence=0.9, target="t2"),
        ]
    )
    out = generate(rep, dry_run=False)
    assert len(out.pr_urls) == 1
    assert any("boom" in s.get("reason", "") for s in out.skipped)
