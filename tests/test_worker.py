"""Tests for director/worker.py — the always-on claim_next_step poller.

Mirrors the patterns in test_factory_droid.py and test_stress_director.py.
No live MCP, no live Neon — all I/O is stubbed.

We use the synchronous ``_tick`` async helper directly so tests are fast
and deterministic. The polling loop (``_run_async``) is exercised once
via ``max_ticks=1``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from director import act as act_mod
from director import worker as worker_mod
from director.act import fleet_run_id_var

# ── helpers ───────────────────────────────────────────────────────────


@pytest.fixture
def captured(monkeypatch):
    art_mock = MagicMock(return_value=MagicMock(close=lambda: None))
    dec_mock = MagicMock(return_value=MagicMock(close=lambda: None))
    monkeypatch.setattr("director.act.fleet.record_artifact", art_mock)
    monkeypatch.setattr("director.act.fleet.record_decision", dec_mock)

    def _fake_fire(coro):
        if hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(act_mod, "_fire_and_forget", _fake_fire)
    return {"artifact": art_mock, "decision": dec_mock}


def _claimed_row(
    *,
    row_id: int = 101,
    repo: str = "lkmotto/motto-sdr-agent",
    kind: str = "factory_droid",
    title: str = "test droid",
    prompt: str = "do the thing",
) -> dict:
    """Build a fake pending_moves row in the shape returned by claim_next_step."""
    return {
        "id": row_id,
        "repo": repo,
        "kind": kind,
        "title": title,
        "rationale": "test",
        "intent": "smoke",
        "priority": 2,
        "move_payload": {
            "repo": repo,
            "kind": kind,
            "title": title,
            "rationale": "test",
            "intent": "smoke",
            "priority": 2,
            "prompt_for_claude_code": prompt,
        },
        "status": "claimed",
    }


@pytest.fixture
def mock_mcp(monkeypatch):
    """Replace _claim_via_mcp / _release_via_mcp with in-memory stubs.

    Returns a dict whose ``claims`` key is the list of rows the next
    claim call should return, and ``released`` is the list of
    (move_id, reason) tuples observed.
    """
    state: dict = {"claims": [], "released": [], "mark_applied": [], "mark_failed": []}

    async def _claim(runner_id, kinds, limit):
        return state["claims"]

    async def _release(move_id, runner_id, reason):
        state["released"].append((move_id, reason))

    def _mark_applied(move_id, *, detail=""):
        state["mark_applied"].append((move_id, detail))
        return True

    def _mark_failed(move_id, *, detail):
        state["mark_failed"].append((move_id, detail))
        return True

    monkeypatch.setattr(worker_mod, "_claim_via_mcp", _claim)
    monkeypatch.setattr(worker_mod, "_release_via_mcp", _release)
    monkeypatch.setattr(worker_mod.queue, "mark_applied", _mark_applied)
    monkeypatch.setattr(worker_mod.queue, "mark_failed", _mark_failed)
    return state


# ── env knob tests ────────────────────────────────────────────────────


def test_poll_seconds_defaults_and_minimum(monkeypatch):
    monkeypatch.delenv("DIRECTOR_WORKER_POLL_SECS", raising=False)
    assert worker_mod._poll_seconds() == 30
    monkeypatch.setenv("DIRECTOR_WORKER_POLL_SECS", "0")
    assert worker_mod._poll_seconds() == 5  # min floor
    monkeypatch.setenv("DIRECTOR_WORKER_POLL_SECS", "120")
    assert worker_mod._poll_seconds() == 120
    monkeypatch.setenv("DIRECTOR_WORKER_POLL_SECS", "garbage")
    assert worker_mod._poll_seconds() == 30  # fallback


def test_batch_size_clamps_to_mcp_contract(monkeypatch):
    monkeypatch.delenv("DIRECTOR_WORKER_BATCH", raising=False)
    assert worker_mod._batch_size() == 3
    monkeypatch.setenv("DIRECTOR_WORKER_BATCH", "100")
    assert worker_mod._batch_size() == 10  # capped at MCP max
    monkeypatch.setenv("DIRECTOR_WORKER_BATCH", "0")
    assert worker_mod._batch_size() == 1  # floor


def test_kinds_defaults_to_factory_droid(monkeypatch):
    monkeypatch.delenv("DIRECTOR_WORKER_KINDS", raising=False)
    assert worker_mod._kinds() == ["factory_droid"]
    monkeypatch.setenv("DIRECTOR_WORKER_KINDS", "factory_droid,merge_pr , file_issue")
    assert worker_mod._kinds() == ["factory_droid", "merge_pr", "file_issue"]


# ── tick behavior ─────────────────────────────────────────────────────


def test_tick_empty_queue_returns_zero_counters(mock_mcp):
    counters = asyncio.run(worker_mod._tick("test-runner", ["factory_droid"], 3))
    assert counters == {"claimed": 0, "applied": 0, "failed": 0, "released": 0}


def test_tick_dispatches_factory_droid_and_marks_applied(mock_mcp, captured, monkeypatch):
    monkeypatch.setenv("FACTORY_API_KEY", "fake-key")
    fleet_run_id_var.set("worker-test")
    mock_mcp["claims"] = [_claimed_row(row_id=101)]

    async def _stub_spawn(prompt, tags):
        return {"session_id": "sess_worker_01", "raw": {}}

    monkeypatch.setattr(act_mod, "_factory_spawn_async", _stub_spawn)

    counters = asyncio.run(worker_mod._tick("test-runner", ["factory_droid"], 3))
    assert counters["claimed"] == 1
    assert counters["applied"] == 1
    assert counters["failed"] == 0
    assert mock_mcp["mark_applied"] == [(101, "https://factory.ai/sessions/sess_worker_01")]
    assert mock_mcp["mark_failed"] == []
    assert mock_mcp["released"] == []


def test_tick_releases_skipped_row_back_to_approved(mock_mcp, captured, monkeypatch):
    """Skipped status (e.g. missing FACTORY_API_KEY) → release, not fail.

    The whole point is to avoid burning a row on a transient config gap.
    Operator fixes the env var, next worker tick re-claims and runs.
    """
    monkeypatch.delenv("FACTORY_API_KEY", raising=False)  # forces skip
    fleet_run_id_var.set("worker-test")
    mock_mcp["claims"] = [_claimed_row(row_id=202)]

    counters = asyncio.run(worker_mod._tick("test-runner", ["factory_droid"], 3))
    assert counters["claimed"] == 1
    assert counters["applied"] == 0
    assert counters["failed"] == 0
    assert counters["released"] == 1
    assert mock_mcp["released"][0][0] == 202
    assert mock_mcp["mark_applied"] == []
    assert mock_mcp["mark_failed"] == []


def test_tick_marks_failed_on_error_status(mock_mcp, captured, monkeypatch):
    monkeypatch.setenv("FACTORY_API_KEY", "fake-key")
    fleet_run_id_var.set("worker-test")
    mock_mcp["claims"] = [_claimed_row(row_id=303)]

    async def _stub_spawn(prompt, tags):
        raise ValueError("factory 503")

    monkeypatch.setattr(act_mod, "_factory_spawn_async", _stub_spawn)

    counters = asyncio.run(worker_mod._tick("test-runner", ["factory_droid"], 3))
    assert counters["failed"] == 1
    assert mock_mcp["mark_failed"][0][0] == 303
    assert "error" in mock_mcp["mark_failed"][0][1].lower()
    assert mock_mcp["mark_applied"] == []
    assert mock_mcp["released"] == []


def test_tick_handles_three_concurrent_rows_without_crosstalk(mock_mcp, captured, monkeypatch):
    """Multiple rows in one tick: each gets its own session id and outcome.

    Mirrors stress test C, but for the worker code path specifically.
    Catches any state leakage between rows in a single tick.
    """
    monkeypatch.setenv("FACTORY_API_KEY", "fake-key")
    fleet_run_id_var.set("worker-test")
    mock_mcp["claims"] = [
        _claimed_row(row_id=400, prompt="task-0"),
        _claimed_row(row_id=401, prompt="task-1"),
        _claimed_row(row_id=402, prompt="task-2"),
    ]
    seen_prompts: list[str] = []

    async def _stub_spawn(prompt, tags):
        seen_prompts.append(prompt)
        return {"session_id": f"sess_{prompt}", "raw": {}}

    monkeypatch.setattr(act_mod, "_factory_spawn_async", _stub_spawn)

    counters = asyncio.run(worker_mod._tick("test-runner", ["factory_droid"], 3))
    assert counters["applied"] == 3
    assert sorted(rid for rid, _ in mock_mcp["mark_applied"]) == [400, 401, 402]
    assert seen_prompts == ["task-0", "task-1", "task-2"]


def test_tick_marks_failed_when_row_to_move_blows_up(mock_mcp, captured, monkeypatch):
    """A malformed row must NOT poison the queue or crash the tick.

    The handoff doc warned about this — if row_to_move raises, the
    worker has to mark the row failed (not just leave it claimed) so
    the queue can make progress on the rest.
    """
    bad_row = {"id": 999}  # missing required keys → row_to_move raises
    mock_mcp["claims"] = [bad_row]

    counters = asyncio.run(worker_mod._tick("test-runner", ["factory_droid"], 3))
    # Tick treats this as a claimed-but-could-not-build situation —
    # mark_failed was called, the dispatch loop saw no moves.
    assert mock_mcp["mark_failed"][0][0] == 999
    assert "row_to_move" in mock_mcp["mark_failed"][0][1]
    assert counters["claimed"] == 1
    assert counters["applied"] == 0


# ── loop entry ────────────────────────────────────────────────────────


def test_run_async_max_ticks_one_returns_after_single_iteration(mock_mcp, monkeypatch):
    """The poll loop honors max_ticks so tests aren't infinite.

    Also confirms the loop calls _tick even when the queue is empty.
    """
    # Speed up the trailing sleep so the test runs in well under a second.
    monkeypatch.setenv("DIRECTOR_WORKER_POLL_SECS", "5")  # floored to min

    tick_calls = {"n": 0}

    real_tick = worker_mod._tick

    async def _counting_tick(*args, **kwargs):
        tick_calls["n"] += 1
        return await real_tick(*args, **kwargs)

    monkeypatch.setattr(worker_mod, "_tick", _counting_tick)

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(worker_mod.asyncio, "sleep", _no_sleep)

    ticks = asyncio.run(worker_mod._run_async(max_ticks=1))
    assert ticks == 1
    assert tick_calls["n"] == 1


def test_tick_survives_act_raising_exception(mock_mcp, captured, monkeypatch):
    """If act() raises (not just returns error), the OUTER loop catches
    it so the polling loop survives. This test exercises _run_async's
    try/except wrapping by monkeypatching act() to raise on the first
    call, then succeed on the second.
    """
    monkeypatch.setenv("DIRECTOR_WORKER_POLL_SECS", "5")

    # Replace the trailing inter-tick sleep with a no-op coroutine so
    # the test runs in well under a second. Avoid lambda-capturing
    # asyncio.sleep — that creates infinite recursion via the patched
    # reference.
    async def _no_sleep(_):
        return None

    monkeypatch.setattr(worker_mod.asyncio, "sleep", _no_sleep)

    call_state = {"n": 0}

    def _raising_tick_then_succeed(*args, **kwargs):
        call_state["n"] += 1
        if call_state["n"] == 1:
            raise RuntimeError("simulated tick blowup")

        async def _ok():
            return {"claimed": 0, "applied": 0, "failed": 0, "released": 0}

        return _ok()

    monkeypatch.setattr(worker_mod, "_tick", _raising_tick_then_succeed)

    # Two ticks: first raises, second succeeds. Loop must complete both.
    ticks = asyncio.run(worker_mod._run_async(max_ticks=2))
    assert ticks == 2
    assert call_state["n"] == 2
