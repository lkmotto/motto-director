"""Unit tests for director.epics + director.epic_executor.

DB-free: epics module and epic_executor are exercised via dependency
injection / monkeypatch since they normally talk to Postgres.
"""

from __future__ import annotations

from director import epic_executor
from director.epics import Epic, EpicStep
from director.ideate import NextMove


def _make_epic(steps: list[EpicStep], *, epic_id: int = 1) -> Epic:
    return Epic(
        id=epic_id,
        title="Boost AMC panels",
        kpi_ref="AMC panel registrations",
        rationale="Gap is huge",
        estimated_cycles=5,
        success_criteria="42 panels live",
        steps=steps,
        status="active",
    )


def test_epic_plan_payload_round_trip():
    steps = [EpicStep(order=1, title="audit", kind="file_issue", repo="lkmotto/x")]
    e = _make_epic(steps)
    payload = e.plan_payload()
    assert payload["kpi_ref"] == "AMC panel registrations"
    assert payload["steps"][0]["order"] == 1
    assert payload["steps"][0]["depends_on"] == []


def test_executor_picks_first_step_when_nothing_applied(monkeypatch):
    steps = [
        EpicStep(order=1, title="step one", kind="spawn_session", repo="lkmotto/x"),
        EpicStep(order=2, title="step two", kind="spawn_session", repo="lkmotto/x", depends_on=[1]),
    ]
    epic = _make_epic(steps, epic_id=42)
    monkeypatch.setattr(epic_executor, "list_active_epics", lambda: [epic])
    monkeypatch.setattr(epic_executor, "applied_step_orders", lambda eid: set())

    moves = epic_executor.queue_next_steps()
    assert len(moves) == 1
    m = moves[0]
    assert isinstance(m, NextMove)
    assert m.epic_id == 42
    assert m.step_order == 1
    assert "step one" in m.title


def test_executor_skips_blocked_step(monkeypatch):
    """If step 2 depends on step 1 and step 1 is not yet applied, step 2 is
    blocked and the executor should pick step 1 first.
    """
    steps = [
        EpicStep(
            order=2, title="needs one", kind="spawn_session", repo="lkmotto/x", depends_on=[1]
        ),
        EpicStep(order=1, title="root", kind="spawn_session", repo="lkmotto/x"),
    ]
    epic = _make_epic(steps)
    monkeypatch.setattr(epic_executor, "list_active_epics", lambda: [epic])
    monkeypatch.setattr(epic_executor, "applied_step_orders", lambda eid: set())
    moves = epic_executor.queue_next_steps()
    assert len(moves) == 1
    assert moves[0].step_order == 1


def test_executor_advances_after_step_applied(monkeypatch):
    steps = [
        EpicStep(order=1, title="root", kind="spawn_session", repo="lkmotto/x"),
        EpicStep(order=2, title="next", kind="spawn_session", repo="lkmotto/x", depends_on=[1]),
    ]
    epic = _make_epic(steps)
    monkeypatch.setattr(epic_executor, "list_active_epics", lambda: [epic])
    monkeypatch.setattr(epic_executor, "applied_step_orders", lambda eid: {1})
    moves = epic_executor.queue_next_steps()
    assert len(moves) == 1
    assert moves[0].step_order == 2


def test_executor_returns_empty_when_done(monkeypatch):
    steps = [EpicStep(order=1, title="only", kind="spawn_session", repo="lkmotto/x")]
    epic = _make_epic(steps)
    monkeypatch.setattr(epic_executor, "list_active_epics", lambda: [epic])
    monkeypatch.setattr(epic_executor, "applied_step_orders", lambda eid: {1})
    moves = epic_executor.queue_next_steps()
    assert moves == []


def test_executor_caps_active_epics(monkeypatch):
    monkeypatch.setenv("DIRECTOR_MAX_ACTIVE_EPICS", "2")
    epics = [
        _make_epic(
            [EpicStep(order=1, title=f"e{i}", kind="file_issue", repo="lkmotto/x")],
            epic_id=i,
        )
        for i in range(1, 6)
    ]
    monkeypatch.setattr(epic_executor, "list_active_epics", lambda: epics)
    monkeypatch.setattr(epic_executor, "applied_step_orders", lambda eid: set())
    moves = epic_executor.queue_next_steps()
    assert len(moves) == 2  # capped


def test_planner_parser_validates_shape(monkeypatch):
    """orchestrator._parse_planner_epics rejects malformed planner output."""
    from director import orchestrator

    good = (
        '{"epics":[{"title":"t","kpi_ref":"AMC panel registrations",'
        '"rationale":"r","estimated_cycles":3,"success_criteria":"s",'
        '"steps":['
        '{"order":1,"title":"a","kind":"file_issue",'
        '"repo":"lkmotto/x","rationale":"r"},'
        '{"order":2,"title":"b","kind":"spawn_session",'
        '"repo":"lkmotto/x","rationale":"r","depends_on":[1]},'
        '{"order":3,"title":"c","kind":"spawn_session",'
        '"repo":"lkmotto/x","rationale":"r","depends_on":[2]}'
        "]}]}"
    )
    epics = orchestrator._parse_planner_epics(good, run_id="r1")
    assert len(epics) == 1
    assert len(epics[0].steps) == 3
    assert epics[0].steps[1].depends_on == [1]


def test_planner_parser_drops_too_few_steps():
    from director import orchestrator

    bad = (
        '{"epics":[{"title":"t","kpi_ref":"k","steps":['
        '{"order":1,"title":"only","kind":"file_issue","repo":"lkmotto/x"}]}]}'
    )
    assert orchestrator._parse_planner_epics(bad, run_id="r1") == []


def test_planner_parser_handles_empty():
    from director import orchestrator

    assert orchestrator._parse_planner_epics("", run_id="r1") == []
    assert orchestrator._parse_planner_epics("not json", run_id="r1") == []
    assert orchestrator._parse_planner_epics('{"epics":[]}', run_id="r1") == []
