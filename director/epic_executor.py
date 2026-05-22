"""Epic executor — picks the next concrete move from each active epic.

Runs every cycle (cheap; one DB read, no LLM calls). For each row in
``epics`` with status='active', it finds the lowest-order step that doesn't
already have a corresponding pending_move (per ``epics.applied_step_orders``)
and produces a NextMove with ``epic_id`` + ``step_order`` set. Those moves
flow through the standard fanout merge -> queue -> approval pipeline.

The executor is intentionally dumb. It does NOT re-plan, re-rank, or
synthesize new prompts — it just hands the next planned step to the human
gate. The planner lens (orchestrator.LENS_PROMPTS['planner']) is what
proposes epics in the first place.

Why split planner from executor:
  * Planner is heavy + LLM-driven, runs heavy-only and proposes 3-step
    plans across many cycles.
  * Executor is cheap + deterministic, runs every cycle.
  * Each step still goes through the human approval gate, so a bad plan
    can be killed move-by-move without abandoning the whole epic.

Caps:
  * DIRECTOR_MAX_ACTIVE_EPICS (default 3) bounds concurrent epics.
  * One move per epic per cycle (the next step). Caller can choose to
    skip steps whose dependencies aren't satisfied yet.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime

from director.epics import Epic, applied_step_orders, list_active_epics
from director.ideate import NextMove


def _log(event: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


def _max_active_epics() -> int:
    raw = os.environ.get("DIRECTOR_MAX_ACTIVE_EPICS", "3")
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


def _next_step(epic: Epic):
    """Pick the lowest-order step whose dependencies are satisfied and which
    hasn't already been queued/applied.
    """
    if epic.id is None:
        return None
    seen = applied_step_orders(epic.id)
    by_order = sorted(epic.steps, key=lambda s: s.order)
    for step in by_order:
        if step.order in seen:
            continue
        # Dependency check: every depends_on order must be in `seen`.
        if any(d not in seen for d in step.depends_on if d is not None):
            continue
        return step
    return None


def _step_to_move(epic: Epic, step) -> NextMove:
    title = step.title or f"{epic.title} — step {step.order}"
    rationale = (
        f"Epic #{epic.id} '{epic.title}' (KPI: {epic.kpi_ref}) — "
        f"step {step.order} of {len(epic.steps)}: {step.rationale}"
    ).strip()[:1000]
    intent = (f"Multi-cycle epic targeting KPI '{epic.kpi_ref}'. Step {step.order}: {step.title}")[
        :500
    ]
    prompt = ""
    if step.kind == "spawn_session":
        prompt = (
            f"You are executing step {step.order} of epic #{epic.id} "
            f"'{epic.title}'.\n\n"
            f"KPI being moved: {epic.kpi_ref}\n"
            f"Success criteria for the epic: {epic.success_criteria}\n\n"
            f"This specific step:\n  Title: {step.title}\n"
            f"  Rationale: {step.rationale}\n"
            f"  Repo: {step.repo}\n\n"
            "Make the smallest, safest change that advances this step. "
            "Open a PR titled with the step title and reference the epic "
            f"as 'Epic #{epic.id}'."
        )
    return NextMove(
        repo=step.repo or epic.steps[0].repo if epic.steps else "",
        kind=step.kind or "spawn_session",  # type: ignore[arg-type]
        title=title[:200],
        rationale=rationale,
        prompt_for_claude_code=prompt,
        priority=2,
        intent=intent,
        code_changes=[],
        epic_id=epic.id,
        step_order=step.order,
    )


def queue_next_steps() -> list[NextMove]:
    """Return one NextMove per active epic with an unstarted next step.

    Caller (orchestrator.parallel_ideate) merges these with lens output via
    the same dedupe rules. The unique index on pending_moves
    (repo, kind, lower(title)) protects against duplicates if the executor
    runs twice in close succession.
    """
    epics = list_active_epics()
    cap = _max_active_epics()
    if len(epics) > cap:
        _log("epic_executor.cap_exceeded", active=len(epics), cap=cap)
        epics = epics[:cap]

    moves: list[NextMove] = []
    for epic in epics:
        step = _next_step(epic)
        if step is None:
            _log(
                "epic_executor.epic_idle",
                epic_id=epic.id,
                title=epic.title,
                reason="no eligible step (done or all-blocked)",
            )
            continue
        move = _step_to_move(epic, step)
        moves.append(move)
        _log(
            "epic_executor.step_queued",
            epic_id=epic.id,
            step_order=step.order,
            kind=step.kind,
            repo=step.repo,
        )

    _log(
        "epic_executor.done",
        active_epics=len(epics),
        moves=len(moves),
    )
    return moves
