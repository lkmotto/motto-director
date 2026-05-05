"""Tests for ideate.py — provider chain + parsing.

The Anthropic adapter is exercised via a MagicMock fixture (it bypasses the
provider chain via `client=`). The OpenAI-compatible adapters (deepseek,
groq, openrouter) are exercised via respx mocks against their concrete
base URLs.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import respx

from director.ideate import ideate
from director.perceive import (
    Issue,
    NorthflankJobStatus,
    PullRequest,
    RepoState,
    Snapshot,
)


def _fake_anthropic(payload: dict | str) -> MagicMock:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=20),
    )
    client = MagicMock()
    client.messages.create.return_value = response
    return client


def _snapshot() -> Snapshot:
    return Snapshot(
        captured_at="2026-04-27T00:00:00Z",
        repos=[
            RepoState(
                repo="lkmotto/motto-appraisal-pipeline",
                open_prs=[
                    PullRequest(
                        repo="lkmotto/motto-appraisal-pipeline",
                        number=12,
                        title="Bump pyarrow",
                        url="x",
                        age_hours=72.0,
                        ci_status="success",
                        review_state="approved",
                        approvals=1,
                        labels=["auto-merge-ok"],
                        head_sha="sha1",
                    )
                ],
                open_issues=[
                    Issue(
                        repo="lkmotto/motto-appraisal-pipeline",
                        number=99,
                        title="Cockpit cannot reach pipeline",
                        url="x",
                        age_hours=200.0,
                        labels=["bug", "P1"],
                    )
                ],
                default_branch="main",
                head_age_hours=24.0,
            )
        ],
        pipeline_auto_nudge=NorthflankJobStatus(
            job="pipeline-auto-nudge",
            last_run_at="2026-04-26T00:00:00Z",
            last_run_status="failed",
        ),
    )


_MOVES_PAYLOAD = {
    "moves": [
        {
            "repo": "lkmotto/motto-sdr-agent",
            "kind": "file_issue",
            "title": "X",
            "rationale": "y",
            "prompt_for_claude_code": "",
            "priority": 3,
            "intent": "Issue #99 stalled 200h; filing tracking issue.",
        }
    ]
}


def _openai_compatible_response(payload: dict) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(payload)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
    }


def _log_events(captured_out: str) -> list[dict]:
    return [
        json.loads(ln)
        for ln in captured_out.splitlines()
        if ln.strip().startswith("{")
    ]


def test_ideate_parses_moves_and_keeps_only_those_with_intent():
    payload = {
        "moves": [
            {
                "repo": "lkmotto/motto-appraisal-pipeline",
                "kind": "merge_pr",
                "title": "Bump pyarrow",
                "rationale": "Trivial dependency bump, CI green, approved.",
                "prompt_for_claude_code": "",
                "priority": 1,
                "intent": (
                    "PR #12 has been green and approved for 72h with "
                    "auto-merge-ok; merging unblocks the pipeline release."
                ),
            },
            {
                "repo": "lkmotto/motto-appraisal-pipeline",
                "kind": "spawn_session",
                "title": "Investigate cockpit→pipeline outage",
                "rationale": "P1 bug, 200h old.",
                "prompt_for_claude_code": "Reproduce issue #99 and propose a fix.",
                "priority": 2,
                "intent": (
                    "Issue #99 (P1) has been open 200h with no PR; "
                    "pipeline-auto-nudge job last failed."
                ),
            },
            {
                "repo": "lkmotto/motto-appraisal-pipeline",
                "kind": "nudge_pipeline",
                "title": "Retry pipeline tick",
                "rationale": "Last run failed.",
                "prompt_for_claude_code": "",
                "priority": 3,
                # missing intent → must be dropped
            },
            {
                "repo": "lkmotto/motto-appraisal-pipeline",
                "kind": "noop",
                "title": "",
                "rationale": "",
                "prompt_for_claude_code": "",
                "priority": 5,
                "intent": "Everything else is healthy.",
            },
        ]
    }
    client = _fake_anthropic(payload)
    moves = ideate(_snapshot(), client=client)
    assert [m.kind for m in moves] == ["merge_pr", "spawn_session", "noop"]
    assert moves[0].priority == 1
    assert moves[1].prompt_for_claude_code.startswith("Reproduce")
    assert all(m.intent for m in moves)


def test_ideate_handles_fenced_json_response():
    payload = (
        "```json\n"
        + json.dumps(
            {
                "moves": [
                    {
                        "repo": "lkmotto/motto-sdr-agent",
                        "kind": "file_issue",
                        "title": "Drip cadence regression",
                        "rationale": "weekend skip",
                        "prompt_for_claude_code": "",
                        "priority": 4,
                        "intent": "Filing weekend-skip regression as an issue.",
                    }
                ]
            }
        )
        + "\n```"
    )
    client = _fake_anthropic(payload)
    moves = ideate(_snapshot(), client=client)
    assert len(moves) == 1
    assert moves[0].kind == "file_issue"


def test_ideate_returns_empty_on_unparseable_response():
    client = _fake_anthropic("not json at all")
    assert ideate(_snapshot(), client=client) == []


def test_provider_chain_default_is_claude_max_first(monkeypatch):
    """PR #24 made claude_max the canonical primary provider. PR
    switching-claude-max-to-subprocess kept it primary, just changed transport."""
    from director.ideate import _provider_chain

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert _provider_chain() == [
        "claude_max",
        "deepseek",
        "groq",
        "openrouter",
        "anthropic",
    ]


def test_provider_chain_respects_llm_provider_env(monkeypatch):
    from director.ideate import _provider_chain

    monkeypatch.setenv("LLM_PROVIDER", "groq")
    assert _provider_chain() == [
        "groq",
        "claude_max",
        "deepseek",
        "openrouter",
        "anthropic",
    ]

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    assert _provider_chain() == [
        "openrouter",
        "claude_max",
        "deepseek",
        "groq",
        "anthropic",
    ]

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert _provider_chain() == [
        "anthropic",
        "claude_max",
        "deepseek",
        "groq",
        "openrouter",
    ]


def test_provider_chain_unknown_value_falls_back_to_canonical(monkeypatch):
    from director.ideate import _provider_chain

    monkeypatch.setenv("LLM_PROVIDER", "bogus")
    assert _provider_chain() == [
        "claude_max",
        "deepseek",
        "groq",
        "openrouter",
        "anthropic",
    ]


def test_ideate_uses_deepseek_first_when_key_present(monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")

    with respx.mock() as mock:
        mock.post("https://api.deepseek.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_compatible_response(_MOVES_PAYLOAD))
        )
        moves = ideate(_snapshot())

    assert len(moves) == 1
    events = _log_events(capsys.readouterr().out)
    used = [e for e in events if e["event"] == "ideate.provider_used"]
    assert used and used[0]["provider"] == "deepseek"
    assert used[0]["tokens_in"] == 11 and used[0]["tokens_out"] == 22


def test_ideate_fails_over_from_deepseek_401_to_groq(monkeypatch, capsys):
    # claude_max is first in the chain but unavailable without a token, so it
    # logs a failover with status_code=None (missing-key skip). Failover under
    # test is deepseek (401) → groq (200), which is the second failover entry.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-bad")
    monkeypatch.setenv("GROQ_API_KEY", "groq-good")

    with respx.mock() as mock:
        mock.post("https://api.deepseek.com/v1/chat/completions").mock(
            return_value=httpx.Response(401, json={"error": "no credit"})
        )
        mock.post("https://api.groq.com/openai/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_compatible_response(_MOVES_PAYLOAD))
        )
        moves = ideate(_snapshot())

    assert len(moves) == 1
    events = _log_events(capsys.readouterr().out)
    failovers = [e for e in events if e["event"] == "ideate.provider_failover"]
    assert len(failovers) >= 2
    # First failover: claude_max → deepseek (missing-token skip).
    assert failovers[0]["from"] == "claude_max"
    assert failovers[0]["status_code"] is None
    # Second failover: deepseek (401) → groq.
    deepseek_fo = next(e for e in failovers if e["from"] == "deepseek")
    assert deepseek_fo["to"] == "groq"
    assert deepseek_fo["status_code"] == 401
    used = [e for e in events if e["event"] == "ideate.provider_used"]
    assert used and used[0]["provider"] == "groq"


def test_ideate_skips_provider_with_missing_key(monkeypatch, capsys):
    """Missing-key failover logs without a status_code and continues to the
    next provider that has a key.

    With claude_max first in the chain (PR #24), expected failover order when
    only OPENROUTER_API_KEY is set: claude_max → deepseek → groq → openrouter."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-ok")

    with respx.mock() as mock:
        mock.post("https://openrouter.ai/api/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_compatible_response(_MOVES_PAYLOAD))
        )
        moves = ideate(_snapshot())

    assert len(moves) == 1
    events = _log_events(capsys.readouterr().out)
    failovers = [e for e in events if e["event"] == "ideate.provider_failover"]
    # claude_max, deepseek, and groq should all fail over with status_code=None.
    assert [f["from"] for f in failovers[:3]] == ["claude_max", "deepseek", "groq"]
    assert all(f["status_code"] is None for f in failovers[:3])
    used = [e for e in events if e["event"] == "ideate.provider_used"]
    assert used and used[0]["provider"] == "openrouter"


def test_ideate_returns_empty_when_no_provider_can_serve(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert ideate(_snapshot()) == []


def test_response_shape_parity_across_openai_compatible_adapters(monkeypatch):
    """Same OpenAI-compatible response shape parses identically for
    deepseek, groq, and openrouter."""
    expected = ["lkmotto/motto-sdr-agent"]
    for provider, base_url in [
        ("deepseek", "https://api.deepseek.com/v1"),
        ("groq", "https://api.groq.com/openai/v1"),
        ("openrouter", "https://openrouter.ai/api/v1"),
    ]:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        key_env = {
            "deepseek": "DEEPSEEK_API_KEY",
            "groq": "GROQ_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }[provider]
        monkeypatch.setenv(key_env, "k")
        monkeypatch.setenv("LLM_PROVIDER", provider)

        with respx.mock() as mock:
            mock.post(f"{base_url}/chat/completions").mock(
                return_value=httpx.Response(
                    200, json=_openai_compatible_response(_MOVES_PAYLOAD)
                )
            )
            moves = ideate(_snapshot())

        assert [m.repo for m in moves] == expected, f"shape mismatch for {provider}"


# ── Claude Max via `claude` CLI subprocess ────────────────────────────────────
#
# As of Jan 9, 2026 Anthropic blocks subscription OAuth from /v1/messages.
# `_call_claude_max` now shells out to the `claude` CLI binary instead.
# These tests cover: missing token, missing binary, success, non-zero exit,
# bad JSON output, timeout. They do NOT mock httpx — the new path doesn't
# use httpx at all.

from director import ideate as ideate_mod  # noqa: E402


def test_call_claude_max_unavailable_when_token_missing(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    try:
        ideate_mod._call_claude_max("sys", "user")
    except ideate_mod._ProviderUnavailable as exc:
        assert "CLAUDE_CODE_OAUTH_TOKEN" in str(exc)
    else:
        raise AssertionError("expected _ProviderUnavailable")


def test_call_claude_max_unavailable_when_binary_missing(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-tok")
    monkeypatch.setenv("CLAUDE_CLI_BIN", "definitely-not-a-real-binary-zzz")
    try:
        ideate_mod._call_claude_max("sys", "user")
    except ideate_mod._ProviderUnavailable as exc:
        assert "definitely-not-a-real-binary-zzz" in str(exc)
    else:
        raise AssertionError("expected _ProviderUnavailable")


def test_call_claude_max_subprocess_success(monkeypatch):
    """Happy path: CLI exits 0, emits JSON with `result` text + usage."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-tok")

    cli_payload = json.dumps(
        {
            "result": '{"moves": []}',
            "model": "claude-sonnet-4-5",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
    )

    captured_cmd: list[str] = []
    captured_kwargs: dict = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        captured_cmd.extend(cmd)
        captured_kwargs.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=cli_payload, stderr="")

    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setattr("subprocess.run", fake_run)

    result = ideate_mod._call_claude_max("system-prompt", "user-msg")

    assert result.text == '{"moves": []}'
    assert result.tokens_in == 100
    assert result.tokens_out == 50
    # Critical: --append-system-prompt (preserves Claude Code identity), not --system-prompt
    assert "--append-system-prompt" in captured_cmd
    assert "--system-prompt" not in captured_cmd
    assert "system-prompt" in captured_cmd
    # User msg is positional after -p
    p_idx = captured_cmd.index("-p")
    assert captured_cmd[p_idx + 1] == "user-msg"
    # JSON output requested
    assert "--output-format" in captured_cmd
    assert captured_cmd[captured_cmd.index("--output-format") + 1] == "json"
    # Regression for prod failure observed 2026-05-05 23:00 UTC: the CLI
    # was reading inherited stdin in the Northflank cron environment, hitting
    # the "no stdin data received in 3s" warning then exit=1. Pinning stdin to
    # DEVNULL is the documented fix.
    import subprocess as _sp  # noqa: PLC0415
    assert captured_kwargs.get("stdin") == _sp.DEVNULL


def test_call_claude_max_subprocess_nonzero_exit(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-tok")
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    try:
        ideate_mod._call_claude_max("sys", "user")
    except ideate_mod._ProviderHTTPError as exc:
        assert exc.status_code == 502
        assert "boom" in exc.reason
    else:
        raise AssertionError("expected _ProviderHTTPError")


def test_call_claude_max_subprocess_bad_json(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-tok")
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="not json", stderr=""),
    )
    try:
        ideate_mod._call_claude_max("sys", "user")
    except ideate_mod._ProviderHTTPError as exc:
        assert exc.status_code == 502
        assert "bad JSON" in exc.reason
    else:
        raise AssertionError("expected _ProviderHTTPError")


def test_call_claude_max_subprocess_timeout(monkeypatch):
    import subprocess  # noqa: PLC0415

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-tok")
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")

    def fake_run(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd=["claude"], timeout=120)

    monkeypatch.setattr("subprocess.run", fake_run)

    try:
        ideate_mod._call_claude_max("sys", "user")
    except ideate_mod._ProviderHTTPError as exc:
        assert exc.status_code == 504
        assert "timeout" in exc.reason.lower()
    else:
        raise AssertionError("expected _ProviderHTTPError")
