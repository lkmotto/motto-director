"""Stress tests for the director loop.

These are deterministic load/edge-case tests added before flipping the
worker loop to full-auto. They live alongside the regular pytest suite
so they run in CI on every push — the "stress" is in *what* they
exercise (concurrent cycle locks, large queues, malformed payloads,
deprecated kinds, repeat-offender escalation), not in wall-clock time
or live infrastructure.

Tests are organized by scenario, mirroring the stress plan documented in
the session handoff:

  A. Parallel cycle invocations — Postgres advisory lock semantics
  B. Synthetic repeat-offender critiques → propose_epic must fire
  C. Many approved pending_moves → multiple droids race claim_next_step
  D. spawn_session injection — deprecated kind dropped at parse time
  E. Malformed propose_epic payload — dispatcher returns ActResult(error)
     without crashing the cycle or poisoning the queue.

None of these tests hit live infrastructure. Postgres, Factory, and
the MCP server are all mocked with the same patterns used elsewhere
in the suite (see ``tests/test_concurrency.py`` and
``tests/test_factory_droid.py``).
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from director import act as act_mod
from director import critic as critic_mod
from director import main as main_mod
from director.act import act, fleet_run_id_var
from director.critic import (
    CritiqueResult,
    _build_self_heal_epic_move,
    _detect_repeat_offenders,
)
from director.ideate import NextMove, _coerce_move
from director.perceive import Snapshot

# ── shared helpers ────────────────────────────────────────────────────


@pytest.fixture
def captured(monkeypatch):
    """Stub fleet.record_artifact / record_decision so act() is offline.

    Mirrors tests/test_factory_droid.py — same pattern, same return shape.
    """
    art_mock = MagicMock(return_value=MagicMock(close=lambda: None))
    dec_mock = MagicMock(return_value=MagicMock(close=lambda: None))
    monkeypatch.setattr("director.act.fleet.record_artifact", art_mock)
    monkeypatch.setattr("director.act.fleet.record_decision", dec_mock)

    def _fake_fire(coro):
        if hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(act_mod, "_fire_and_forget", _fake_fire)
    return {"artifact": art_mock, "decision": dec_mock}


def _empty_snapshot() -> Snapshot:
    return Snapshot(captured_at="2026-05-22T00:00:00Z", repos=[])


def _patch_psycopg_advisory_lock(*, acquired: bool):
    """Build a context-manager psycopg mock for advisory-lock acquisition.

    Each cursor().execute('SELECT pg_try_advisory_lock(%s)') returns a
    single row [acquired_bool]. The mock supports the autocommit-style
    connection main.py uses (no `with conn` block around the connect()
    itself — the conn is stashed on _CYCLE_LOCK_CONN and released later).
    """
    cur = MagicMock()
    cur.fetchone.return_value = [acquired]
    cur_cm = MagicMock()
    cur_cm.__enter__.return_value = cur
    cur_cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cur_cm
    fake = MagicMock()
    fake.connect.return_value = conn
    return fake, conn


# ── A. Parallel cycle invocations ─────────────────────────────────────


def test_stress_A_concurrent_cycle_lock_only_one_winner(monkeypatch):
    """5 threads race to acquire the cycle lock; only 1 should win.

    The director uses Postgres ``pg_try_advisory_lock`` to serialize
    cycles. With the lock held, subsequent cycles must skip (return
    True from a second caller is fatal — that would mean two cycles
    ideating against the same snapshot and racing on pending_moves).

    We simulate this by giving the first call a True row and every
    subsequent call a False row. The contract: exactly one caller
    sees the lock as acquired.
    """
    monkeypatch.setenv("NEON_DATABASE_URL", "postgres://stress")

    call_count = {"n": 0}
    lock = threading.Lock()

    def _connect(*args, **kwargs):
        # Each connect returns its own conn whose first execute reports
        # whether it won the lock. Thread-safe counter so the simulation
        # is deterministic regardless of OS scheduling.
        with lock:
            call_count["n"] += 1
            winner = call_count["n"] == 1
        cur = MagicMock()
        cur.fetchone.return_value = [winner]
        cur_cm = MagicMock()
        cur_cm.__enter__.return_value = cur
        cur_cm.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur_cm
        return conn

    fake_psycopg = MagicMock()
    fake_psycopg.connect.side_effect = _connect

    results: list[bool] = []
    results_lock = threading.Lock()

    def _attempt():
        # Each thread gets its own stash slot — main.py uses a module-
        # level dict which is fine in cron (single process) but in this
        # test we just observe the boolean return.
        with patch.dict("sys.modules", {"psycopg": fake_psycopg}):
            # Each call replaces the stash; that's OK for the assertion
            # here because we're only checking the return value. The
            # real release path runs in _try_release_cycle_lock, which
            # we don't need to exercise for this assertion.
            ok = main_mod._try_acquire_cycle_lock(holder=f"t{threading.get_ident()}")
        with results_lock:
            results.append(ok)

    threads = [threading.Thread(target=_attempt) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one caller won the advisory lock. The other four saw
    # row[0] == False and returned False.
    assert results.count(True) == 1, results
    assert results.count(False) == 4, results
    # Clean up the stashed winner connection so the next test starts fresh.
    main_mod._CYCLE_LOCK_CONN.pop("conn", None)


def test_stress_A_cycle_lock_falls_open_without_neon(monkeypatch):
    """No NEON_DATABASE_URL → lock is a no-op (legacy single-cron behavior).

    This is the safety belt: if Neon is misconfigured we never deadlock
    the cron — we silently allow the cycle. The trade-off is documented
    in main.py: at most one cron should run per Northflank service.
    """
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main_mod._try_acquire_cycle_lock(holder="fallback") is True


# ── B. Repeat-offender critiques → propose_epic ───────────────────────


def _critique(verdict: str, **overrides) -> CritiqueResult:
    base = dict(
        artifact_id=1,
        agent_name="motto-sdr-agent",
        kind="cold_email",
        verdict=verdict,
        severity="medium" if verdict != "pass" else None,
        issues=["pushy CTA", "no opt-out"],
        suggested_fix="soften tone, add unsubscribe",
        repo="lkmotto/motto-sdr-agent",
        intent="cold outreach to lenders",
        send_blocking=verdict == "block",
        tokens_in=100,
        tokens_out=50,
        latency_ms=800,
    )
    base.update(overrides)
    return CritiqueResult(**base)


def test_stress_B_three_flags_in_same_repo_kind_triggers_epic():
    """Weighted failures >= 3 on (repo, kind) → repeat offender.

    Threshold = 3, block weight = 2, flag weight = 1. Three flags hit
    threshold exactly. The detector must surface this group so
    critique_artifacts() can promote it to a propose_epic move.
    """
    results = [
        _critique("flag", artifact_id=1),
        _critique("flag", artifact_id=2),
        _critique("flag", artifact_id=3),
    ]
    offenders = _detect_repeat_offenders(results)
    assert len(offenders) == 1
    repo, kind, weighted, group = offenders[0]
    assert repo == "lkmotto/motto-sdr-agent"
    assert kind == "cold_email"
    assert weighted == 3
    assert len(group) == 3


def test_stress_B_two_blocks_trigger_epic_via_double_weight():
    """Block verdicts count double. 2 blocks = weighted 4, over threshold.

    This catches the case where a producer is failing hard (block, not
    flag) and we'd otherwise have to wait for the 3rd failure — the
    fix is more expensive than the 1-cycle latency saving.
    """
    results = [_critique("block", artifact_id=1), _critique("block", artifact_id=2)]
    offenders = _detect_repeat_offenders(results)
    assert len(offenders) == 1
    _, _, weighted, _ = offenders[0]
    assert weighted == 4


def test_stress_B_below_threshold_does_not_trigger_epic():
    """A single flag must NOT promote to an epic — that's just noise."""
    offenders = _detect_repeat_offenders([_critique("flag")])
    assert offenders == []


def test_stress_B_propose_epic_payload_is_well_formed_json():
    """Move payload must round-trip through json.loads — the dispatcher
    in act._propose_epic re-parses it. If the critic ever serializes a
    non-JSON-safe field this catches it before prod.
    """
    group = [_critique("flag", artifact_id=i) for i in (1, 2, 3)]
    move = _build_self_heal_epic_move("lkmotto/motto-sdr-agent", "cold_email", 3, group)
    assert move is not None
    assert move.kind == "propose_epic"
    assert move.code_changes
    payload_raw = move.code_changes[0]["content"]
    payload = json.loads(payload_raw)  # must not raise
    assert payload["epic_title"]
    assert payload["kpi_ref"].startswith("output_critic_pass_rate:")
    assert len(payload["steps"]) >= 1
    # The dispatcher expects each step to have at least order/title/kind/repo.
    for s in payload["steps"]:
        assert {"order", "title", "kind", "repo"}.issubset(s.keys())


# ── C. Many approved pending_moves race claim_next_step ───────────────


def test_stress_C_act_dispatches_ten_factory_droids_without_crosstalk(captured, monkeypatch):
    """Queue 10 factory_droid moves; act() must dispatch each cleanly.

    This is the *upper bound* of what a single cycle would ever apply
    (DIRECTOR_APPLY_MAX defaults to 5; we go 2x to find any state
    leakage). Each spawn is stubbed to return a unique session id so
    we can assert results don't bleed across droids.
    """
    monkeypatch.setenv("FACTORY_API_KEY", "fake-key")
    fleet_run_id_var.set("c0c0c0c0-0000-0000-0000-000000000000")

    spawn_calls: list[str] = []

    async def _stub_spawn(prompt, tags):
        # Use the prompt as the discriminator so we can verify ordering.
        spawn_calls.append(prompt)
        return {"session_id": f"sess_{len(spawn_calls):02d}", "raw": {}}

    monkeypatch.setattr(act_mod, "_factory_spawn_async", _stub_spawn)

    moves = [
        NextMove(
            repo="lkmotto/motto-sdr-agent",
            kind="factory_droid",
            title=f"droid task {i}",
            rationale="stress",
            prompt_for_claude_code=f"task-{i}",
            priority=2,
            intent=f"intent-{i}",
        )
        for i in range(10)
    ]

    # act() defaults to top_n=5 (cycle budget); raise to 10 so we
    # exercise the full claim_next_step burst, which is the scenario
    # where the new worker loop will hand off N droids in one tick.
    results = act(moves, _empty_snapshot(), top_n=10)

    assert len(results) == 10
    statuses = [r.status for r in results]
    assert statuses.count("executed") == 10, statuses
    # Each result's detail carries a unique session id — no crosstalk.
    session_ids = {r.detail.split("sess_")[-1][:2] for r in results}
    assert len(session_ids) == 10, session_ids
    # And spawn_calls preserves input order.
    assert spawn_calls == [f"task-{i}" for i in range(10)]


def test_stress_C_one_failed_droid_does_not_block_the_rest(captured, monkeypatch):
    """If the 3rd spawn raises, droids 4-10 must still dispatch.

    This is the "one bad apple" test. The act() loop wraps each
    dispatch and records an ``error`` status for the failed move but
    continues iterating — otherwise a single Factory 503 stops the
    whole queue and we silently back up.
    """
    monkeypatch.setenv("FACTORY_API_KEY", "fake-key")
    fleet_run_id_var.set("c0c0c0c0-0000-0000-0000-000000000001")

    # Use a non-RuntimeError to fail one specific move. act.py's
    # _spawn_factory_droid has a RuntimeError fallback that re-runs the
    # spawn in a thread (because it interprets RuntimeError as "event
    # loop already running") — raising ValueError bypasses that and
    # lands directly in the outer except handler.
    async def _stub_spawn(prompt, tags):
        if prompt == "task-3":
            raise ValueError("factory api 503")
        return {"session_id": f"sess_{prompt}", "raw": {}}

    monkeypatch.setattr(act_mod, "_factory_spawn_async", _stub_spawn)

    moves = [
        NextMove(
            repo="lkmotto/motto-sdr-agent",
            kind="factory_droid",
            title=f"droid task {i}",
            rationale="stress",
            prompt_for_claude_code=f"task-{i}",
            priority=2,
            intent=f"intent-{i}",
        )
        for i in range(10)
    ]

    results = act(moves, _empty_snapshot(), top_n=10)
    assert len(results) == 10
    statuses = [r.status for r in results]
    assert statuses.count("error") == 1
    assert statuses.count("executed") == 9


# ── D. spawn_session injection must be dropped ────────────────────────


def test_stress_D_spawn_session_proposal_is_dropped_with_structured_log(
    capsys,
):
    """The planner is not allowed to propose spawn_session anymore.

    _coerce_move must return None and emit ``move.dropped`` with
    ``reason="deprecated_proposal_kind"`` so we can grep for any
    rogue prompts in prod logs.
    """
    raw = {
        "repo": "lkmotto/motto-mcp-server",
        "kind": "spawn_session",
        "title": "injected via prompt leak",
        "rationale": "test",
        "prompt_for_claude_code": "do something Claude-y",
        "priority": 1,
        "intent": "this should never run",
    }
    result = _coerce_move(raw)
    assert result is None
    out = capsys.readouterr().out
    # Validate the structured log shape — one JSON object per line, no prose.
    found = False
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            ev.get("event") == "move.dropped"
            and ev.get("reason") == "deprecated_proposal_kind"
            and ev.get("kind") == "spawn_session"
        ):
            found = True
            break
    assert found, f"expected move.dropped log event; got: {out!r}"


def test_stress_D_factory_droid_still_accepted():
    """Sanity: the inverse — factory_droid still coerces cleanly.

    Catches the failure mode where someone tightens the deprecation
    list too far and accidentally bans the replacement.
    """
    raw = {
        "repo": "lkmotto/motto-sdr-agent",
        "kind": "factory_droid",
        "title": "ok",
        "rationale": "ok",
        "prompt_for_claude_code": "ok",
        "priority": 2,
        "intent": "smoke",
    }
    assert _coerce_move(raw) is not None


# ── E. Malformed propose_epic payload must not poison the queue ───────


def _propose_epic_move(payload_content: str | None) -> NextMove:
    """Build a propose_epic NextMove with the given payload JSON string.

    Pass ``None`` to omit code_changes entirely (the dispatcher must
    handle the missing-payload branch).
    """
    return NextMove(
        repo="lkmotto/motto-sdr-agent",
        kind="propose_epic",
        title="malformed payload stress",
        rationale="stress",
        prompt_for_claude_code="",
        priority=2,
        intent="stress: malformed payload",
        code_changes=(
            [] if payload_content is None else [{"path": "__epic__", "content": payload_content}]
        ),
    )


@pytest.mark.parametrize(
    "payload,expected_detail_fragment",
    [
        (None, "missing code_changes payload"),
        ("", "empty epic payload"),
        ("{not json", "bad JSON payload"),
        ('"just a string"', "payload not an object"),
        ("{}", "no valid steps in payload"),
        ('{"steps": []}', "no valid steps in payload"),
        # Note: {"steps": [{"not_a_step": 1}]} would actually pass because
        # the step builder uses .get() with defaults for every field. The
        # dispatcher is permissive on step shape by design — the cockpit
        # reviews proposed epics before activation, so we tolerate slop.
        ('{"steps": [42, "strings", null]}', "no valid steps in payload"),
    ],
)
def test_stress_E_malformed_propose_epic_returns_error_without_crashing(
    captured, monkeypatch, payload, expected_detail_fragment
):
    """Every malformed branch must return ActResult(status='error') —
    never raise. The act() outer loop also catches exceptions, but the
    dispatcher itself should fail closed.

    Importantly: ``insert_epics`` must NOT be called for any of these
    cases. If the dispatcher's validation regresses, this assertion
    catches it — we'd be inserting garbage Epic rows into Neon.
    """
    monkeypatch.setattr(
        act_mod, "insert_epics", MagicMock(side_effect=AssertionError("must not be called"))
    )
    fleet_run_id_var.set("e0e0e0e0-0000-0000-0000-000000000000")

    move = _propose_epic_move(payload)
    results = act([move], _empty_snapshot())

    assert len(results) == 1
    r = results[0]
    assert r.status == "error", r.detail
    assert expected_detail_fragment in r.detail


def test_stress_E_valid_propose_epic_calls_insert_epics_once(captured, monkeypatch):
    """The happy path control: a valid payload calls insert_epics once.

    Without this, the negative tests above might trivially pass by
    insert_epics never being reachable for any input shape.
    """
    fake_insert = MagicMock(return_value={"inserted": 1, "updated": 0})
    monkeypatch.setattr(act_mod, "insert_epics", fake_insert)
    fleet_run_id_var.set("e0e0e0e0-0000-0000-0000-000000000001")

    payload = json.dumps(
        {
            "epic_title": "stress: well-formed epic",
            "kpi_ref": "output_critic_pass_rate:repo:kind",
            "rationale": "valid",
            "estimated_cycles": 3,
            "success_criteria": "stress test",
            "steps": [
                {
                    "order": 1,
                    "title": "step 1",
                    "kind": "factory_droid",
                    "repo": "lkmotto/motto-sdr-agent",
                    "rationale": "ok",
                }
            ],
        }
    )
    move = _propose_epic_move(payload)
    results = act([move], _empty_snapshot())

    assert results[0].status == "executed", results[0].detail
    assert fake_insert.call_count == 1


# ── meta: total move-kind coverage sanity ─────────────────────────────


def test_stress_meta_self_heal_threshold_constants_unchanged():
    """Guard rail against silent threshold drift. If someone bumps
    these constants we want to surface it in PR review, not in a
    runtime surprise where the epic-promotion rate triples overnight.
    """
    assert critic_mod._SELF_HEAL_FAILURE_THRESHOLD == 3
    assert critic_mod._BLOCK_WEIGHT == 2
