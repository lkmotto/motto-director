"""Tests for director.quality.synthesizer — rule-based fix detection."""

from __future__ import annotations

from director.quality.synthesizer import (
    CONFIDENCE_HIGH_ERROR,
    CONFIDENCE_LOW_MERGE,
    CONFIDENCE_OVERTURN,
    CONFIDENCE_REPEATED_FAILURE,
    CONFIDENCE_REVERT,
    CONFIDENCE_SLOW,
    QualityReport,
    SuggestedFix,
    synthesize,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _empty_lf() -> dict:
    return {"configured": False}


def _empty_pg() -> dict:
    return {"configured": False}


def _empty_gh() -> dict:
    return {"configured": False}


# ── unconfigured signals → empty report ─────────────────────────────────────


def test_synthesize_all_unconfigured_returns_empty_report():
    r = synthesize(_empty_lf(), _empty_pg(), _empty_gh())
    assert isinstance(r, QualityReport)
    assert r.suggested_fixes == []
    assert r.top_problems == []
    assert r.confidence_score == 0.0
    assert r.signals["langfuse_configured"] is False
    assert r.signals["postgres_configured"] is False
    assert r.signals["github_configured"] is False


# ── langfuse detectors ──────────────────────────────────────────────────────


def test_synthesize_high_error_rate_emits_cheap_config_tweak():
    lf = {
        "configured": True,
        "trace_count": 50,
        "error_rate": {"toolA": 0.25, "toolB": 0.05},
        "per_tool_latency": {},
    }
    r = synthesize(lf, _empty_pg(), _empty_gh())
    fixes = [f for f in r.suggested_fixes if f.target == "toolA"]
    assert len(fixes) == 1
    assert fixes[0].cheapness == "cheap"
    assert fixes[0].fix_kind == "config_tweak"
    assert fixes[0].confidence == CONFIDENCE_HIGH_ERROR
    # toolB at 5% is below threshold and should not appear
    assert not any(f.target == "toolB" for f in r.suggested_fixes)


def test_synthesize_slow_p95_emits_timeout_bump():
    lf = {
        "configured": True,
        "trace_count": 10,
        "error_rate": {},
        "per_tool_latency": {"slow_tool": {"p95": 45_000.0, "p50": 1000.0}},
    }
    r = synthesize(lf, _empty_pg(), _empty_gh())
    assert any(
        f.target == "slow_tool" and f.fix_kind == "config_tweak"
        and f.confidence == CONFIDENCE_SLOW
        for f in r.suggested_fixes
    )


# ── postgres detectors ──────────────────────────────────────────────────────


def test_synthesize_repeated_failure_legacy_shape_is_expensive():
    pg = {
        "configured": True,
        "task_count": 100,
        "decisions_overturned_count": 0,
        "repeated_failures": [
            {"tool": "comp_hunter", "error": "TimeoutError", "count": 5},
        ],
    }
    r = synthesize(_empty_lf(), pg, _empty_gh())
    matched = [f for f in r.suggested_fixes if f.target == "comp_hunter"]
    assert len(matched) == 1
    assert matched[0].cheapness == "expensive"
    assert matched[0].fix_kind == "flag_for_review"
    assert matched[0].confidence == CONFIDENCE_REPEATED_FAILURE


def test_synthesize_repeated_failure_pattern_shape_also_works():
    """postgres_signals.collect() returns {pattern, count} not {tool,error}."""
    pg = {
        "configured": True,
        "task_count": 100,
        "repeated_failures": [
            {"pattern": "ConnectionError: timeout", "count": 7},
        ],
    }
    r = synthesize(_empty_lf(), pg, _empty_gh())
    assert any(
        "ConnectionError: timeout" in f.title for f in r.suggested_fixes
    )


def test_synthesize_low_count_repeated_failure_skipped():
    pg = {
        "configured": True,
        "task_count": 100,
        "repeated_failures": [
            {"tool": "comp_hunter", "error": "x", "count": 1},  # below min
        ],
    }
    r = synthesize(_empty_lf(), pg, _empty_gh())
    assert all(f.target != "comp_hunter" for f in r.suggested_fixes)


def test_synthesize_overturn_rate_from_count_emits_prompt_edit():
    pg = {
        "configured": True,
        "task_count": 50,
        "decisions_overturned_count": 15,  # 30% > 20% threshold
        "repeated_failures": [],
    }
    r = synthesize(_empty_lf(), pg, _empty_gh())
    matched = [f for f in r.suggested_fixes if f.fix_kind == "prompt_edit"]
    assert len(matched) == 1
    assert matched[0].target == "director.ideate"
    assert matched[0].cheapness == "cheap"
    assert matched[0].confidence == CONFIDENCE_OVERTURN


# ── github detectors ────────────────────────────────────────────────────────


def test_synthesize_low_merge_rate_emits_expensive_review():
    gh = {
        "configured": True,
        "merge_rate": 0.30,
        "per_repo": {
            "lkmotto/motto-director": {"pr_count": 10},
        },
        "reverted_pr_numbers": [],
    }
    r = synthesize(_empty_lf(), _empty_pg(), gh)
    matched = [f for f in r.suggested_fixes if f.target == "fleet"
               and "merge rate" in f.title.lower()]
    assert len(matched) == 1
    assert matched[0].cheapness == "expensive"
    assert matched[0].confidence == CONFIDENCE_LOW_MERGE


def test_synthesize_revert_emits_max_confidence_review():
    gh = {
        "configured": True,
        "merge_rate": 1.0,
        "per_repo": {"lkmotto/motto-director": {"pr_count": 1}},
        "reverted_pr_numbers": [{"repo": "x", "number": 42, "title": "Revert foo"}],
    }
    r = synthesize(_empty_lf(), _empty_pg(), gh)
    matched = [f for f in r.suggested_fixes if "Reverted" in f.title]
    assert len(matched) == 1
    assert matched[0].cheapness == "expensive"
    assert matched[0].confidence == CONFIDENCE_REVERT


def test_synthesize_low_merge_skipped_below_pr_floor():
    """<5 PRs is too few to draw a merge-rate conclusion."""
    gh = {
        "configured": True,
        "merge_rate": 0.20,
        "per_repo": {"r": {"pr_count": 3}},
        "reverted_pr_numbers": [],
    }
    r = synthesize(_empty_lf(), _empty_pg(), gh)
    assert not any("merge rate" in f.title.lower() for f in r.suggested_fixes)


# ── dedupe + ordering ───────────────────────────────────────────────────────


def test_synthesize_dedupes_same_target_and_kind_keeping_highest_confidence():
    # Build a synthetic fix list via two duplicate-emitting paths:
    # langfuse error_rate AND a separate manual stuffing through high
    # latency keyed on the same tool would result in two distinct
    # (target, fix_kind) pairs (config_tweak each, but different tools)
    # so duplicate dedupe is best tested via the helper directly.
    from director.quality.synthesizer import _dedupe

    a = SuggestedFix(
        title="A1", rationale="", confidence=0.5, cheapness="cheap",
        target="x", fix_kind="config_tweak", payload={},
    )
    a2 = SuggestedFix(
        title="A2", rationale="", confidence=0.9, cheapness="cheap",
        target="x", fix_kind="config_tweak", payload={},
    )
    b = SuggestedFix(
        title="B", rationale="", confidence=0.7, cheapness="cheap",
        target="y", fix_kind="config_tweak", payload={},
    )
    out = _dedupe([a, a2, b])
    titles = {f.title for f in out}
    assert "A2" in titles
    assert "A1" not in titles
    assert "B" in titles
    # sorted desc by confidence
    assert out[0].confidence >= out[-1].confidence


def test_synthesize_top_problems_capped_at_three_and_overall_confidence_is_mean():
    lf = {
        "configured": True,
        "trace_count": 100,
        "error_rate": {f"t{i}": 0.5 for i in range(5)},
        "per_tool_latency": {},
    }
    r = synthesize(lf, _empty_pg(), _empty_gh())
    assert len(r.top_problems) == 3
    expected = sum(f.confidence for f in r.suggested_fixes) / len(
        r.suggested_fixes
    )
    assert abs(r.confidence_score - expected) < 1e-9


def test_synthesize_signals_summary_has_stable_keys():
    r = synthesize(_empty_lf(), _empty_pg(), _empty_gh())
    for k in (
        "langfuse_configured",
        "postgres_configured",
        "github_configured",
        "trace_count",
        "pr_count_total",
    ):
        assert k in r.signals
