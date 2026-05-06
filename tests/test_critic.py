"""Tests for the output critic lens."""

from __future__ import annotations

import asyncio

from director import critic
from director.critic import (
    CRITIC_SYSTEM_PROMPT,
    CritiqueResult,
    _build_user_message,
    _coerce_verdict,
    _extract_json,
    _result_to_move,
    _verdict_to_status,
    is_enabled,
)
from director.ideate import _VALID_KINDS

# ── Static / config sanity ────────────────────────────────────────────


def test_file_critique_issue_kind_registered():
    """The act dispatcher only handles kinds in _VALID_KINDS; if the
    critic emits a kind that isn't registered, _coerce_move silently
    drops it. Guard against that regression here.
    """
    assert "file_critique_issue" in _VALID_KINDS


def test_critic_system_prompt_has_strict_json():
    assert "STRICT JSON" in CRITIC_SYSTEM_PROMPT
    assert "verdict" in CRITIC_SYSTEM_PROMPT
    assert "block" in CRITIC_SYSTEM_PROMPT
    assert "pass" in CRITIC_SYSTEM_PROMPT


def test_is_enabled_default_off(monkeypatch):
    monkeypatch.delenv("DIRECTOR_OUTPUT_CRITIC", raising=False)
    assert is_enabled() is False


def test_is_enabled_truthy(monkeypatch):
    for v in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("DIRECTOR_OUTPUT_CRITIC", v)
        assert is_enabled() is True


def test_is_enabled_falsy(monkeypatch):
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("DIRECTOR_OUTPUT_CRITIC", v)
        assert is_enabled() is False


def test_verdict_to_status_mapping():
    assert _verdict_to_status("pass") == "passed"
    assert _verdict_to_status("flag") == "flagged"
    assert _verdict_to_status("block") == "blocked"
    # Unknown verdicts default to flagged (safer than passing).
    assert _verdict_to_status("garbage") == "flagged"


# ── JSON extraction (LLMs love fences and prose) ──────────────────────


def test_extract_json_strips_fences():
    assert _extract_json('```json\n{"verdict": "pass"}\n```') == {"verdict": "pass"}
    assert _extract_json('{"verdict": "flag"}') == {"verdict": "flag"}
    assert _extract_json("not json at all") == {}
    assert _extract_json("") == {}


def test_extract_json_handles_prose_wrapper():
    text = 'Here is my judgment: {"verdict": "block", "issues": ["x"]} done.'
    parsed = _extract_json(text)
    assert parsed.get("verdict") == "block"


# ── Verdict coercion ──────────────────────────────────────────────────


def test_coerce_verdict_happy_path():
    raw = {
        "verdict": "flag",
        "severity": "high",
        "issues": ["typo in CTA", "missing signoff"],
        "suggested_fix": "rewrite intro paragraph",
    }
    v, sev, issues, fix = _coerce_verdict(raw)
    assert v == "flag"
    assert sev == "high"
    assert issues == ["typo in CTA", "missing signoff"]
    assert fix == "rewrite intro paragraph"


def test_coerce_verdict_unknown_verdict_defaults_to_flag():
    """If the LLM returns garbage we should NOT call it 'pass' (would
    auto-approve send-blocking artifacts). Defensively coerce to flag."""
    v, sev, _, _ = _coerce_verdict({"verdict": "looks_great"})
    assert v == "flag"
    assert sev == "low"  # since not pass, default severity is low


def test_coerce_verdict_pass_leaves_severity_none():
    v, sev, _, _ = _coerce_verdict({"verdict": "pass"})
    assert v == "pass"
    assert sev is None


def test_coerce_verdict_clips_long_issues():
    long_issue = "x" * 500
    _, _, issues, _ = _coerce_verdict({
        "verdict": "block",
        "issues": [long_issue],
    })
    assert len(issues[0]) == 200


def test_coerce_verdict_caps_issue_count():
    _, _, issues, _ = _coerce_verdict({
        "verdict": "flag",
        "issues": [f"i{n}" for n in range(50)],
    })
    assert len(issues) == 20


# ── User message construction ─────────────────────────────────────────


def test_build_user_message_includes_intent_and_body():
    art = {
        "id": 42,
        "agent_name": "motto-sdr-agent",
        "kind": "cold_email",
        "name": "lender-outreach-batch-1",
        "content": {
            "body": "Hi Pat,\n\nWould you like an appraisal?",
            "intent": "lender outreach",
            "repo": "lkmotto/motto-sdr-agent",
            "send_blocking": True,
            "truncated": False,
        },
    }
    msg = _build_user_message(art)
    assert "42" in msg
    assert "motto-sdr-agent" in msg
    assert "cold_email" in msg
    assert "lender outreach" in msg
    assert "Would you like an appraisal" in msg
    assert "send_blocking: True" in msg


def test_build_user_message_truncates_long_body():
    art = {
        "id": 1,
        "agent_name": "x",
        "kind": "y",
        "content": {"body": "a" * (critic.CRITIC_BODY_CHAR_CAP + 1000)},
    }
    msg = _build_user_message(art)
    assert "[...truncated for critic prompt]" in msg


# ── Move generation ───────────────────────────────────────────────────


def _make_result(**overrides) -> CritiqueResult:
    base = dict(
        artifact_id=7,
        agent_name="motto-sdr-agent",
        kind="cold_email",
        verdict="flag",
        severity="medium",
        issues=["pushy CTA", "no opt-out"],
        suggested_fix="soften tone, add unsubscribe",
        repo="lkmotto/motto-sdr-agent",
        intent="cold outreach to lenders",
        send_blocking=True,
        tokens_in=100,
        tokens_out=50,
        latency_ms=800,
    )
    base.update(overrides)
    return CritiqueResult(**base)


def test_pass_verdict_emits_no_move():
    """The whole point of `pass` — no GH issue noise for clean outputs."""
    assert _result_to_move(_make_result(verdict="pass", severity=None)) is None


def test_flag_verdict_emits_move_with_findings():
    move = _result_to_move(_make_result())
    assert move is not None
    assert move.kind == "file_critique_issue"
    assert move.repo == "lkmotto/motto-sdr-agent"
    assert "FLAG" in move.title
    assert "cold_email" in move.title
    assert "#7" in move.title
    assert "pushy CTA" in move.rationale
    assert "no opt-out" in move.rationale
    assert "soften tone" in move.rationale
    # Producing agent + send_blocking is in either rationale or intent
    # so the receiving repo's reviewer has full context.
    assert "send_blocking" in move.rationale or "send_blocking" in move.intent


def test_block_verdict_priority_is_highest():
    move = _result_to_move(_make_result(verdict="block", severity="high"))
    assert move is not None
    assert move.priority == 1


def test_flag_low_severity_priority_lower_than_flag_high():
    high = _result_to_move(_make_result(verdict="flag", severity="high"))
    low = _result_to_move(_make_result(verdict="flag", severity="low"))
    assert high is not None and low is not None
    assert high.priority < low.priority


def test_no_repo_drops_move():
    """Defensive: without a source repo we have nowhere to file an issue.
    The critique still got written back via mark_artifact_reviewed; we
    just don't emit a move this tick.
    """
    assert _result_to_move(_make_result(repo="")) is None


# ── End-to-end: critique_artifacts with mocked MCP + DeepSeek ─────────


def _async_return(value):
    async def _f(*args, **kwargs):
        return value
    return _f


def test_critique_artifacts_no_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    moves = asyncio.run(critic.critique_artifacts())
    assert moves == []


def test_critique_artifacts_no_pending(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.setattr(critic, "_fetch_pending", _async_return([]))
    moves = asyncio.run(critic.critique_artifacts())
    assert moves == []


def test_critique_artifacts_vision_capability_gap(monkeypatch):
    """Image-bearing kinds should get a capability_gap critique without
    calling DeepSeek (doctrine: report capability gaps, don't fall back).

    The gap-check lives INSIDE `_critique_one` (it returns early before
    any HTTP call), so we let the real implementation run and just
    monkeypatch the network-touching boundaries.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    pending = [{
        "id": 1,
        "agent_name": "motto-video-agent",
        "kind": "thumbnail",
        "name": "ep-7-thumb",
        "content": {
            "body": "(image bytes elided)",
            "intent": "thumbnail for episode 7",
            "repo": "lkmotto/motto-video-agent",
            "send_blocking": False,
        },
    }]
    monkeypatch.setattr(critic, "_fetch_pending", _async_return(pending))
    monkeypatch.setattr(critic, "_mark_reviewed", _async_return(True))

    # Block any actual outbound HTTP — if the gap-check fails to
    # short-circuit, this would explode and we'd see it in the test.
    import httpx

    async def _no_post(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError(
            "DeepSeek HTTP must not be called for vision-required kinds"
        )
    monkeypatch.setattr(httpx.AsyncClient, "post", _no_post)

    moves = asyncio.run(critic.critique_artifacts())
    assert len(moves) == 1
    move = moves[0]
    assert move.kind == "file_critique_issue"
    assert "capability_gap" in move.rationale.lower()


def test_critique_artifacts_pass_emits_no_move(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    pending = [{
        "id": 99,
        "agent_name": "motto-sdr-agent",
        "kind": "cold_email",
        "content": {
            "body": "Hi! Quick question about your pipeline.",
            "intent": "lender outreach",
            "repo": "lkmotto/motto-sdr-agent",
            "send_blocking": True,
        },
    }]
    monkeypatch.setattr(critic, "_fetch_pending", _async_return(pending))
    monkeypatch.setattr(critic, "_mark_reviewed", _async_return(True))

    # Stub _critique_one to return a passing result without network.
    async def _stub(client, *, artifact, api_key, model):
        return CritiqueResult(
            artifact_id=int(artifact["id"]),
            agent_name=artifact["agent_name"],
            kind=artifact["kind"],
            verdict="pass",
            severity=None,
            issues=[],
            suggested_fix="",
            repo=artifact["content"]["repo"],
            intent=artifact["content"]["intent"],
            send_blocking=True,
            tokens_in=10,
            tokens_out=5,
            latency_ms=100,
        )
    monkeypatch.setattr(critic, "_critique_one", _stub)

    moves = asyncio.run(critic.critique_artifacts())
    assert moves == []  # passing artifacts produce no moves


def test_critique_artifacts_block_emits_high_priority_move(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    pending = [{
        "id": 7,
        "agent_name": "motto-appraisal-pipeline",
        "kind": "amc_reply_draft",
        "content": {
            "body": "Sure, signing now.",
            "intent": "reply to AMC inquiry",
            "repo": "lkmotto/motto-appraisal-pipeline",
            "send_blocking": True,
        },
    }]
    monkeypatch.setattr(critic, "_fetch_pending", _async_return(pending))
    monkeypatch.setattr(critic, "_mark_reviewed", _async_return(True))

    async def _stub(client, *, artifact, api_key, model):
        return CritiqueResult(
            artifact_id=int(artifact["id"]),
            agent_name=artifact["agent_name"],
            kind=artifact["kind"],
            verdict="block",
            severity="high",
            issues=["violates AMC doctrine: must not auto-sign"],
            suggested_fix="route to human review queue; do not auto-send",
            repo=artifact["content"]["repo"],
            intent=artifact["content"]["intent"],
            send_blocking=True,
            tokens_in=20,
            tokens_out=15,
            latency_ms=300,
        )
    monkeypatch.setattr(critic, "_critique_one", _stub)

    moves = asyncio.run(critic.critique_artifacts())
    assert len(moves) == 1
    m = moves[0]
    assert m.kind == "file_critique_issue"
    assert m.priority == 1  # block = highest priority
    assert "BLOCK" in m.title
    assert "AMC doctrine" in m.rationale
    assert "human review" in m.rationale
