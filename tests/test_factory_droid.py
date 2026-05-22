"""Tests for the factory_droid move kind dispatch in director/act.py.

Mirrors test_iocapture.py's spawn_session coverage. Verifies:
- The new MoveKind is accepted by ideate's _coerce_move
- act() dispatches factory_droid moves to _spawn_factory_droid
- Missing FACTORY_API_KEY produces a 'skipped' result with the no-fallback
  doctrine message (and does NOT fall back to Claude)
- A successful Factory spawn records both prompt + session artifacts and
  a 'spawned_factory_droid' decision
- _resolve_droid_tag routes by repo+intent keywords
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from director import act as act_mod
from director.act import _resolve_droid_tag, act, fleet_run_id_var
from director.ideate import NextMove, _coerce_move
from director.perceive import Snapshot

REPO = "lkmotto/motto-social-agent"


# ---------- helpers (mirror test_iocapture style) ----------


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


def _set_run(monkeypatch):
    monkeypatch.setattr(act_mod, "fleet_run_id_var", fleet_run_id_var)
    fleet_run_id_var.set("11111111-2222-3333-4444-555555555555")


def _droid_move(
    *,
    repo: str = REPO,
    title: str = "Sync DOPPLER tokens across motto-core",
    prompt: str = (
        "Run `doppler secrets get FACTORY_API_KEY` against motto-core/prd and confirm it exists."
    ),
    intent: str = "Doppler audit drift on motto-core",
) -> NextMove:
    return NextMove(
        repo=repo,
        kind="factory_droid",
        title=title,
        rationale="audit doppler drift",
        prompt_for_claude_code=prompt,
        priority=2,
        intent=intent,
    )


def _empty_snapshot() -> Snapshot:
    return Snapshot(captured_at="2026-05-22T00:00:00Z", repos=[])


# ---------- tests ----------


def test_factory_droid_is_valid_movekind():
    """ideate._coerce_move must accept the new kind."""
    raw = {
        "repo": REPO,
        "kind": "factory_droid",
        "title": "test",
        "rationale": "test",
        "prompt_for_claude_code": "do the thing",
        "priority": 2,
        "intent": "smoke",
    }
    move = _coerce_move(raw)
    assert move is not None
    assert move.kind == "factory_droid"


def test_factory_droid_skips_without_api_key(captured, monkeypatch):
    """Per fleet doctrine: when FACTORY_API_KEY is absent we skip explicitly.

    We do NOT silently fall back to Claude Code.
    """
    monkeypatch.delenv("FACTORY_API_KEY", raising=False)
    _set_run(monkeypatch)

    results = act([_droid_move()], _empty_snapshot())

    assert len(results) == 1
    assert results[0].status == "skipped"
    assert "FACTORY_API_KEY" in results[0].detail
    assert "do not fall back" in results[0].detail.lower()
    # No artifacts should be recorded for a pre-spawn skip
    assert captured["artifact"].call_count == 0
    assert captured["decision"].call_count == 0


def test_factory_droid_skips_missing_prompt(captured, monkeypatch):
    monkeypatch.setenv("FACTORY_API_KEY", "fake-key")
    _set_run(monkeypatch)

    move = _droid_move(prompt="")
    results = act([move], _empty_snapshot())
    assert results[0].status == "skipped"
    assert "prompt" in results[0].detail.lower()


def test_factory_droid_records_artifacts_and_decision_on_success(captured, monkeypatch):
    monkeypatch.setenv("FACTORY_API_KEY", "fake-key")
    _set_run(monkeypatch)

    # Stub the async Factory spawn so we don't make real HTTP calls.
    async def _stub_spawn(prompt, tags):
        return {"session_id": "sess_factory_abc", "raw": {}}

    monkeypatch.setattr(act_mod, "_factory_spawn_async", _stub_spawn)

    results = act([_droid_move()], _empty_snapshot())

    assert results[0].status == "executed", results[0].detail
    assert "sess_factory_abc" in results[0].detail

    # Two artifact calls (prompt, then session) + one decision.
    assert captured["artifact"].call_count == 2
    prompt_kwargs = captured["artifact"].call_args_list[0].kwargs
    assert prompt_kwargs["kind"] == "factory_session_prompt"
    assert prompt_kwargs["meta"]["repo"] == REPO
    assert prompt_kwargs["meta"]["droid"]  # tag was resolved

    session_kwargs = captured["artifact"].call_args_list[1].kwargs
    assert session_kwargs["kind"] == "factory_session"
    assert "sess_factory_abc" in session_kwargs["ref"]
    assert session_kwargs["meta"]["session_id"] == "sess_factory_abc"

    decision_kwargs = captured["decision"].call_args_list[0].kwargs
    assert decision_kwargs["choice"] == "spawned_factory_droid"


def test_factory_droid_records_error_on_spawn_failure(captured, monkeypatch):
    monkeypatch.setenv("FACTORY_API_KEY", "fake-key")
    _set_run(monkeypatch)

    async def _stub_spawn(prompt, tags):
        raise RuntimeError("factory api 503")

    monkeypatch.setattr(act_mod, "_factory_spawn_async", _stub_spawn)

    results = act([_droid_move()], _empty_snapshot())
    assert results[0].status == "error"
    assert "factory" in results[0].detail.lower()


# ---------- droid tag routing ----------


@pytest.mark.parametrize(
    "intent,title,repo,expected",
    [
        ("doppler audit", "rotate FACTORY_API_KEY secret", REPO, "doppler-sync"),
        ("redeploy cron", "northflank fleet redeploy", REPO, "northflank-ops"),
        ("fleet audit", "weekly fleet health report", REPO, "ona-fleet-reporter"),
        ("merge PR", "merge PR #42 in motto-mcp-server", "lkmotto/motto-mcp-server", "github-ops"),
        ("plan next step", "design new agent", REPO, "factory-orchestrator"),
    ],
)
def test_resolve_droid_tag_routes_by_keywords(intent, title, repo, expected):
    move = _droid_move(repo=repo, title=title, intent=intent)
    assert _resolve_droid_tag(move) == expected
