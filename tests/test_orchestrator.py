"""Tests for the parallel subagent orchestrator."""

from __future__ import annotations

import asyncio

from director import orchestrator
from director.ideate import NextMove
from director.orchestrator import (
    DEFAULT_LENSES,
    LENS_PROMPTS,
    SubagentResult,
    _extract_json,
    _merge_moves,
    is_enabled,
    parallel_ideate,
)
from director.perceive import Snapshot


def test_default_lenses_have_prompts():
    for lens in DEFAULT_LENSES:
        assert lens in LENS_PROMPTS
        assert "STRICT JSON" in LENS_PROMPTS[lens]
        assert "Hard rules" in LENS_PROMPTS[lens]


def test_extract_json_strips_fences():
    assert _extract_json("```json\n{\"moves\": []}\n```") == {"moves": []}
    assert _extract_json("{\"moves\": [1]}") == {"moves": [1]}
    assert _extract_json("garbage {\"a\": 1} trailing") == {"a": 1}
    assert _extract_json("not json at all") == {}
    assert _extract_json("") == {}


def test_is_enabled_default_off(monkeypatch):
    monkeypatch.delenv("DIRECTOR_PARALLEL_SUBAGENTS", raising=False)
    assert is_enabled() is False


def test_is_enabled_truthy_values(monkeypatch):
    for v in ("1", "true", "yes", "on", "TRUE", "Yes"):
        monkeypatch.setenv("DIRECTOR_PARALLEL_SUBAGENTS", v)
        assert is_enabled() is True


def test_is_enabled_falsy_values(monkeypatch):
    for v in ("0", "false", "no", "off", "", "  "):
        monkeypatch.setenv("DIRECTOR_PARALLEL_SUBAGENTS", v)
        assert is_enabled() is False


def _make_move(repo, kind, title, priority=3, intent="why"):
    return NextMove(
        repo=repo,
        kind=kind,
        title=title,
        rationale="r",
        prompt_for_claude_code="p",
        priority=priority,
        intent=intent,
    )


def test_merge_dedupes_by_repo_kind_title():
    r1 = SubagentResult(
        lens="ci_doctor",
        moves=[_make_move("r1", "merge_pr", "Fix CI", priority=3)],
        tokens_in=0, tokens_out=0, latency_ms=0,
    )
    r2 = SubagentResult(
        lens="stale_pr_closer",
        moves=[_make_move("r1", "merge_pr", "fix ci", priority=2)],
        tokens_in=0, tokens_out=0, latency_ms=0,
    )
    merged = _merge_moves([r1, r2])
    assert len(merged) == 1
    # priority bumped to max(1, min(3, 2) - 1) = 1
    assert merged[0].priority == 1
    # rationale + intent concatenated
    assert "|" in merged[0].rationale
    assert "|" in merged[0].intent


def test_merge_preserves_distinct_moves():
    r1 = SubagentResult(
        lens="ci_doctor",
        moves=[_make_move("r1", "merge_pr", "A", priority=3)],
        tokens_in=0, tokens_out=0, latency_ms=0,
    )
    r2 = SubagentResult(
        lens="stale_pr_closer",
        moves=[_make_move("r2", "merge_pr", "B", priority=2)],
        tokens_in=0, tokens_out=0, latency_ms=0,
    )
    merged = _merge_moves([r1, r2])
    assert len(merged) == 2
    # sorted by priority asc
    assert merged[0].title == "B"
    assert merged[1].title == "A"


def test_merge_caps_at_max_total():
    r = SubagentResult(
        lens="x",
        moves=[
            _make_move("r1", "file_issue", f"t{i}", priority=3)
            for i in range(50)
        ],
        tokens_in=0, tokens_out=0, latency_ms=0,
    )
    merged = _merge_moves([r], max_total=10)
    assert len(merged) == 10


def test_parallel_ideate_no_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    snap = Snapshot(
        captured_at="2026-05-06T00:00:00Z",
        repos=[],
        pipeline_auto_nudge=None,
    )
    out = asyncio.run(parallel_ideate(snap))
    assert out == []


def test_parallel_ideate_handles_subagent_errors(monkeypatch):
    """Even if all subagents error, returns [] (caller falls back)."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    async def fake_call(*args, **kwargs):
        return SubagentResult(
            lens=kwargs["lens"], moves=[], tokens_in=0,
            tokens_out=0, latency_ms=10, error="boom",
        )

    monkeypatch.setattr(orchestrator, "_call_subagent", fake_call)
    snap = Snapshot(
        captured_at="2026-05-06T00:00:00Z",
        repos=[],
        pipeline_auto_nudge=None,
    )
    out = asyncio.run(parallel_ideate(snap))
    assert out == []


def test_parallel_ideate_merges_results(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    async def fake_call(client, *, lens, system, user_msg, api_key, model):
        if lens == "ci_doctor":
            return SubagentResult(
                lens=lens,
                moves=[
                    _make_move(
                        "lkmotto/motto-director",
                        "merge_pr",
                        "Merge stale PR #42",
                        priority=2,
                    )
                ],
                tokens_in=100, tokens_out=50, latency_ms=200,
            )
        return SubagentResult(
            lens=lens, moves=[], tokens_in=10, tokens_out=5,
            latency_ms=100,
        )

    monkeypatch.setattr(orchestrator, "_call_subagent", fake_call)
    snap = Snapshot(
        captured_at="2026-05-06T00:00:00Z",
        repos=[],
        pipeline_auto_nudge=None,
    )
    out = asyncio.run(parallel_ideate(snap))
    assert len(out) == 1
    assert out[0].title == "Merge stale PR #42"
    assert out[0].priority == 2  # no agreement bump (only one lens fired)


def test_parallel_ideate_concurrency_respected(monkeypatch):
    """max_concurrency caps in-flight calls."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    in_flight = {"now": 0, "max": 0}
    lock = asyncio.Lock()

    async def fake_call(client, *, lens, **kwargs):
        async with lock:
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.01)
        async with lock:
            in_flight["now"] -= 1
        return SubagentResult(
            lens=lens, moves=[], tokens_in=0, tokens_out=0,
            latency_ms=10,
        )

    monkeypatch.setattr(orchestrator, "_call_subagent", fake_call)
    snap = Snapshot(
        captured_at="2026-05-06T00:00:00Z",
        repos=[],
        pipeline_auto_nudge=None,
    )
    asyncio.run(
        parallel_ideate(snap, max_concurrency=2)
    )
    # 5 lenses with concurrency=2 → max in-flight should be 2
    assert in_flight["max"] <= 2
    assert in_flight["max"] >= 1
