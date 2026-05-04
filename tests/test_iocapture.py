"""Phase 5.5: I/O capture in spawn_session, merge_pr, compound_pr.

Verifies act helpers call fleet.record_artifact / fleet.record_decision
with the expected kwargs. The act loop is sync, so capture happens via
_fire_and_forget; tests intercept it directly to assert on the args
without needing a running event loop.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
import respx

from director import act as act_mod
from director.act import act, fleet_run_id_var
from director.ideate import NextMove
from director.perceive import PullRequest, RepoState, Snapshot

REPO = "lkmotto/motto-social-agent"
GH = "https://api.github.com"


@pytest.fixture
def captured(monkeypatch):
    """Capture every _fire_and_forget call as (fn_name, kwargs).

    The act helpers call `_fire_and_forget(fleet.record_artifact(...))`,
    so the coroutine is constructed inline. We patch the fleet helpers
    to MagicMocks (returning a sentinel) and capture _fire_and_forget's
    sole positional arg. The coroutine is closed cleanly afterward.
    """
    calls: list[dict] = []

    art_mock = MagicMock(return_value=MagicMock(close=lambda: None))
    dec_mock = MagicMock(return_value=MagicMock(close=lambda: None))

    monkeypatch.setattr("director.act.fleet.record_artifact", art_mock)
    monkeypatch.setattr("director.act.fleet.record_decision", dec_mock)

    def _fake_fire(coro):
        # The coroutine has already been "constructed" by the mock call;
        # nothing to await. Just record what was invoked.
        if hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(act_mod, "_fire_and_forget", _fake_fire)

    return {
        "artifact": art_mock,
        "decision": dec_mock,
        "calls": calls,
    }


def _set_run(monkeypatch):
    monkeypatch.setattr(act_mod, "fleet_run_id_var", fleet_run_id_var)
    fleet_run_id_var.set("11111111-2222-3333-4444-555555555555")


def _spawn_move(
    *,
    repo: str = REPO,
    title: str = "Add retry test",
    prompt: str = "Open tests/test_retry.py and add a test for the timeout edge case.",
) -> NextMove:
    return NextMove(
        repo=repo,
        kind="spawn_session",
        title=title,
        rationale="cover the gap",
        prompt_for_claude_code=prompt,
        priority=2,
        intent="Test gap noticed in cron run NN",
    )


def _empty_snapshot() -> Snapshot:
    return Snapshot(captured_at="2026-05-04T00:00:00Z", repos=[])


def _snapshot_with_pr(pr: PullRequest) -> Snapshot:
    return Snapshot(
        captured_at="2026-05-04T00:00:00Z",
        repos=[
            RepoState(
                repo=pr.repo,
                open_prs=[pr],
                open_issues=[],
                default_branch="main",
            )
        ],
    )


def _eligible_pr(*, title: str = "ci: bump deps") -> PullRequest:
    return PullRequest(
        repo=REPO,
        number=42,
        title=title,
        url=f"https://github.com/{REPO}/pull/42",
        age_hours=2.0,
        ci_status="success",
        review_state="approved",
        approvals=1,
        labels=["director-ok"],
        head_sha="deadbeef",
    )


def test_spawn_session_records_prompt_and_session_artifacts_and_decision(captured, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-tok")
    _set_run(monkeypatch)

    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://claude.ai/api/sessions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "sess_abc123",
                    "session_url": "https://claude.ai/c/sess_abc123",
                },
            )
        )
        results = act([_spawn_move()], _empty_snapshot())

    assert results[0].status == "executed", results[0].detail

    # Two artifact calls: one for prompt (before POST), one for session (after).
    assert captured["artifact"].call_count == 2
    prompt_kwargs = captured["artifact"].call_args_list[0].kwargs
    assert prompt_kwargs["kind"] == "claude_session_prompt"
    assert prompt_kwargs["meta"]["prompt"].startswith("Open tests/")
    assert prompt_kwargs["meta"]["repo"] == REPO

    session_kwargs = captured["artifact"].call_args_list[1].kwargs
    assert session_kwargs["kind"] == "claude_session"
    assert session_kwargs["ref"] == "https://claude.ai/c/sess_abc123"
    assert session_kwargs["meta"]["session_id"] == "sess_abc123"
    assert session_kwargs["meta"]["status_code"] == 200
    assert session_kwargs["meta"]["response_summary"]

    # One decision row.
    assert captured["decision"].call_count == 1
    dkwargs = captured["decision"].call_args.kwargs
    assert dkwargs["choice"] == "spawned_claude_session"
    assert dkwargs["evidence"]["repo"] == REPO
    assert dkwargs["evidence"]["session_url"] == "https://claude.ai/c/sess_abc123"


def test_spawn_session_skipped_by_policy_does_not_capture_io(captured, monkeypatch):
    """Oversized prompts get policy-dropped before the artifact call."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-tok")
    _set_run(monkeypatch)
    huge_prompt = "Edit " + " ".join(f"file_{i}.py" for i in range(10))
    move = _spawn_move(prompt=huge_prompt)

    results = act([move], _empty_snapshot())

    assert results[0].status == "skipped"
    assert "policy" in results[0].detail
    assert captured["artifact"].call_count == 0
    assert captured["decision"].call_count == 0


def test_merge_pr_records_decision_on_success(captured, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    _set_run(monkeypatch)
    pr = _eligible_pr(title="ci: bump deps")
    move = NextMove(
        repo=REPO,
        kind="merge_pr",
        title="ci: bump deps",
        rationale="all green",
        prompt_for_claude_code="",
        priority=1,
        intent="Merge the dep bump now that CI is green.",
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.put(f"{GH}/repos/{REPO}/pulls/42/merge").mock(
            return_value=httpx.Response(200, json={"merged": True})
        )
        results = act([move], _snapshot_with_pr(pr))

    assert results[0].status == "executed"
    assert captured["decision"].call_count == 1
    dkwargs = captured["decision"].call_args.kwargs
    assert dkwargs["choice"] == "merged_pr"
    assert dkwargs["evidence"]["pr_number"] == 42
    assert dkwargs["evidence"]["repo"] == REPO


def test_merge_pr_skipped_records_no_decision(captured, monkeypatch):
    """Policy-skipped merges must NOT record a 'merged_pr' decision."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    _set_run(monkeypatch)
    pr = PullRequest(
        repo=REPO,
        number=42,
        title="ci: bump deps",
        url=f"https://github.com/{REPO}/pull/42",
        age_hours=2.0,
        ci_status="failure",  # blocks merge
        review_state="approved",
        approvals=1,
        labels=["director-ok"],
        head_sha="deadbeef",
    )
    move = NextMove(
        repo=REPO,
        kind="merge_pr",
        title="ci: bump deps",
        rationale="ignore CI",
        prompt_for_claude_code="",
        priority=1,
        intent="Try anyway.",
    )

    results = act([move], _snapshot_with_pr(pr))
    assert results[0].status == "skipped"
    assert captured["decision"].call_count == 0


def test_compound_pr_records_decision_after_append(captured, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.delenv("DIRECTOR_AUTO_MERGE", raising=False)
    monkeypatch.setenv("DIRECTOR_COMPOUND_MAX_MOVES", "10")
    _set_run(monkeypatch)

    move = NextMove(
        repo=REPO,
        kind="compound_pr",
        title="Tighten retry",
        rationale="reduce flakiness",
        prompt_for_claude_code="",
        priority=2,
        intent="Cron run NN observed transient failures.",
        code_changes=[{"path": "src/retry.py", "content": "TIMEOUT = 10\n"}],
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{GH}/repos/{REPO}").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        mock.get(f"{GH}/repos/{REPO}/git/ref/heads/main").mock(
            return_value=httpx.Response(200, json={"object": {"sha": "main-sha"}})
        )
        mock.get(f"{GH}/repos/{REPO}/git/ref/heads/director/auto/compound").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        mock.post(f"{GH}/repos/{REPO}/git/refs").mock(
            return_value=httpx.Response(
                201,
                json={"ref": "refs/heads/director/auto/compound", "object": {"sha": "x"}},
            )
        )
        mock.get(f"{GH}/repos/{REPO}/pulls").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "number": 17,
                        "node_id": "PR_node_17",
                        "html_url": "x",
                        "body": "",
                    }
                ],
            )
        )
        mock.get(f"{GH}/repos/{REPO}/contents/src/retry.py").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        mock.put(f"{GH}/repos/{REPO}/contents/src/retry.py").mock(
            return_value=httpx.Response(201, json={"commit": {"sha": "abc1234"}})
        )
        mock.patch(f"{GH}/repos/{REPO}/pulls/17").mock(
            return_value=httpx.Response(200, json={"number": 17})
        )

        results = act([move], _empty_snapshot())

    assert results[0].status == "executed"
    assert captured["decision"].call_count == 1
    dkwargs = captured["decision"].call_args.kwargs
    assert dkwargs["choice"] == "compound_pr_appended"
    assert dkwargs["evidence"]["repo"] == REPO
    assert dkwargs["evidence"]["pr_number"] == 17
    assert dkwargs["evidence"]["moves_in_pr"] == 1
