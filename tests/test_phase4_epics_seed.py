"""Validates the shape of the Phase-4 seed Epics.

We can't hit Neon in CI, so this test exercises the dataclass constructors
and confirms:
- Both epics build with the expected step counts
- All step.kind values are valid MoveKinds (especially factory_droid!)
- KPI refs match exactly the strings added to motto-kpis.md
- Step dependencies form a DAG (no forward refs, no cycles)
- The 'verify_move' step is last in each epic (matches existing pattern)

If the planner or epic_executor ever drifts, these tests catch it before
the seed script silently no-ops in production.
"""

from __future__ import annotations

from director.ideate import _VALID_KINDS
from scripts.seed_phase4_epics import (
    _apollo_demand_gen_epic,
    _appraisal_turn_time_epic,
)


def test_turn_time_epic_shape():
    epic = _appraisal_turn_time_epic()
    assert "turn-time" in epic.title.lower() or "turn time" in epic.title.lower()
    assert epic.kpi_ref == ("Median appraisal report turn time (intake \u2192 client delivery)")
    assert len(epic.steps) == 5
    assert all(step.kind in _VALID_KINDS for step in epic.steps), (
        f"All step kinds must be valid MoveKinds. Found: {[s.kind for s in epic.steps]}"
    )
    # factory_droid must appear (Phase-2 wiring under test)
    kinds = {step.kind for step in epic.steps}
    assert "factory_droid" in kinds
    # The last step is verify_move (matches the existing pattern in epic_executor)
    assert epic.steps[-1].kind == "verify_move"


def test_apollo_demand_gen_epic_shape():
    epic = _apollo_demand_gen_epic()
    assert "meetings" in epic.title.lower() or "demand-gen" in epic.title.lower()
    assert epic.kpi_ref == "Discovery meetings booked per week"
    assert len(epic.steps) == 5
    assert all(step.kind in _VALID_KINDS for step in epic.steps)
    kinds = {step.kind for step in epic.steps}
    assert "factory_droid" in kinds
    assert epic.steps[-1].kind == "verify_move"


def test_epics_have_acyclic_dependencies():
    """No step.depends_on entry may point to a later step or to itself."""
    for epic in (_appraisal_turn_time_epic(), _apollo_demand_gen_epic()):
        for step in epic.steps:
            for dep in step.depends_on:
                assert dep < step.order, (
                    f"Epic '{epic.title}' step {step.order} depends on {dep} "
                    f"(must be < {step.order})"
                )
                assert dep >= 1, f"Step.depends_on must be >=1, got {dep}"


def test_epics_target_correct_repos():
    """Step.repo must be a real motto-* repo (no typos sneaking past)."""
    valid_repos = {
        "lkmotto/motto-director",
        "lkmotto/motto-mcp-server",
        "lkmotto/motto-sdr-agent",
        "lkmotto/motto-appraisal-pipeline",
        "lkmotto/motto-appraisal-cockpit",
        "lkmotto/comp-hunter",
        "lkmotto/motto-outreach",
    }
    for epic in (_appraisal_turn_time_epic(), _apollo_demand_gen_epic()):
        for step in epic.steps:
            assert step.repo in valid_repos, (
                f"Epic '{epic.title}' step {step.order} targets unknown repo "
                f"'{step.repo}' (not in valid_repos)"
            )


def test_each_step_has_nonempty_rationale():
    """Director.policy requires intent/rationale on every move; epic steps
    feed those, so blank rationales would generate moves the planner drops."""
    for epic in (_appraisal_turn_time_epic(), _apollo_demand_gen_epic()):
        for step in epic.steps:
            assert step.rationale.strip(), (
                f"Epic '{epic.title}' step {step.order} has empty rationale"
            )
            assert len(step.rationale) > 30, (
                f"Epic '{epic.title}' step {step.order} rationale too short: {step.rationale!r}"
            )
