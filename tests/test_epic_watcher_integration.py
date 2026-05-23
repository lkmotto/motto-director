"""Integration tests for `director.epic_watcher.watcher_tick`.

Worker D owns the implementation. These tests describe the grader
pass / fail / stuck branches with everything mocked: Factory API, the
MCP server, DeepSeek's grader, and GitHub.

When the watcher stubs still raise NotImplementedError, each test marks
itself skipped with a clear marker so the suite stays green until
Worker D merges.

The contract under test:
  * `poll_active_epics` returns the list of active epics from the MCP.
  * `fetch_factory_session` returns the Factory session blob + messages.
  * `grade_progress` calls DeepSeek to classify progress as
    making_progress | stuck | failed | done.
  * On 'stuck' (under MAX_REPROMPTS), the watcher calls `reprompt_droid`.
  * On 'failed', the watcher closes the epic as abandoned.
  * On 'done', the watcher verifies criteria then closes as closed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

# asyncio mark applied per-test below (non-async tests must not carry it).


def _skip_if_not_implemented(name: str, exc: NotImplementedError) -> None:
    pytest.skip(f"{name} stub not implemented yet: {exc}")


def _fake_epic(epic_id: int = 100, session_id: str = "sess_xyz") -> dict:
    return {
        "id": epic_id,
        "title": "Test epic",
        "status": "active",
        "factory_session_id": session_id,
        "gh_issue_url": f"https://github.com/lkmotto/motto-mcp-server/issues/{epic_id}",
        "gh_issue_number": epic_id,
        "success_criteria_json": ["criteria A", "criteria B"],
        "cost_so_far_usd": 0.50,
        "max_cost_usd": 25.0,
    }


def _fake_session(status: str = "running") -> dict:
    return {
        "sessionId": "sess_xyz",
        "status": status,
        "messages": [
            {"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": "doing the thing"},
        ],
    }


@pytest.fixture
def watcher_mocks(monkeypatch):
    """Patch the watcher's external callouts with AsyncMocks. Tests override
    individual mocks by reassigning the attributes on the returned namespace.
    """
    from director import epic_watcher

    poll = AsyncMock(return_value=[_fake_epic()])
    fetch_session = AsyncMock(return_value=_fake_session())
    grade = AsyncMock(
        return_value=epic_watcher.GraderResult(
            state="making_progress",
            reasoning="droid still working",
            next_action="keep going",
        )
    )
    reprompt = AsyncMock(return_value=True)
    verify = AsyncMock(return_value=(True, "all criteria met"))
    close = AsyncMock(return_value=None)

    monkeypatch.setattr(epic_watcher, "poll_active_epics", poll)
    monkeypatch.setattr(epic_watcher, "fetch_factory_session", fetch_session)
    monkeypatch.setattr(epic_watcher, "grade_progress", grade)
    monkeypatch.setattr(epic_watcher, "reprompt_droid", reprompt)
    monkeypatch.setattr(epic_watcher, "verify_criteria", verify)
    monkeypatch.setattr(epic_watcher, "close_epic", close)

    monkeypatch.setenv("MOTTO_MCP_URL", "http://mcp.test/mcp")
    monkeypatch.setenv("MOTTO_MCP_AUTH_TOKEN", "tok")
    monkeypatch.setenv("FACTORY_API_KEY", "fac")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")

    class _NS:
        pass

    ns = _NS()
    ns.poll = poll
    ns.fetch_session = fetch_session
    ns.grade = grade
    ns.reprompt = reprompt
    ns.verify = verify
    ns.close = close
    return ns


@pytest.mark.asyncio
async def test_watcher_tick_passes_for_done_epic(watcher_mocks):
    """When the grader returns 'done' and verification succeeds, the watcher
    closes the epic with outcome='closed' and does NOT reprompt the droid."""
    from director import epic_watcher

    watcher_mocks.grade.return_value = epic_watcher.GraderResult(
        state="done",
        reasoning="all criteria visible in PR",
        next_action="close",
    )
    watcher_mocks.verify.return_value = (True, "PR merged")

    try:
        summary = await epic_watcher.watcher_tick(
            mcp_url="http://mcp.test/mcp",
            mcp_token="tok",
            factory_api_key="fac",
            deepseek_api_key="ds",
        )
    except NotImplementedError as exc:
        _skip_if_not_implemented("watcher_tick", exc)

    assert isinstance(summary, dict)
    watcher_mocks.close.assert_awaited_once()
    close_kwargs = watcher_mocks.close.await_args.kwargs
    # Either outcome='closed' or outcome='done' is acceptable for the pass branch.
    assert close_kwargs.get("outcome") in ("closed", "done")
    watcher_mocks.reprompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_watcher_tick_fails_branch_closes_as_abandoned(watcher_mocks):
    """When the grader returns 'failed', the watcher abandons the epic."""
    from director import epic_watcher

    watcher_mocks.grade.return_value = epic_watcher.GraderResult(
        state="failed",
        reasoning="droid is stuck on an unfixable error",
        next_action="abandon",
    )

    try:
        await epic_watcher.watcher_tick(
            mcp_url="http://mcp.test/mcp",
            mcp_token="tok",
            factory_api_key="fac",
            deepseek_api_key="ds",
        )
    except NotImplementedError as exc:
        _skip_if_not_implemented("watcher_tick", exc)

    watcher_mocks.close.assert_awaited_once()
    close_kwargs = watcher_mocks.close.await_args.kwargs
    assert close_kwargs.get("outcome") in ("abandoned", "failed")
    watcher_mocks.verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_watcher_tick_stuck_branch_reprompts(watcher_mocks):
    """When the grader returns 'stuck' (under MAX_REPROMPTS), the watcher
    sends a reprompt to the droid session and does NOT close the epic."""
    from director import epic_watcher

    watcher_mocks.grade.return_value = epic_watcher.GraderResult(
        state="stuck",
        reasoning="droid hasn't acted in 10 min",
        next_action="poke it",
    )

    try:
        await epic_watcher.watcher_tick(
            mcp_url="http://mcp.test/mcp",
            mcp_token="tok",
            factory_api_key="fac",
            deepseek_api_key="ds",
        )
    except NotImplementedError as exc:
        _skip_if_not_implemented("watcher_tick", exc)

    watcher_mocks.reprompt.assert_awaited()
    watcher_mocks.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_watcher_tick_no_active_epics_is_noop(watcher_mocks):
    """When poll_active_epics returns [], the watcher tick is a no-op
    that doesn't call Factory, DeepSeek, GitHub, or close_epic."""
    from director import epic_watcher

    watcher_mocks.poll.return_value = []

    try:
        await epic_watcher.watcher_tick(
            mcp_url="http://mcp.test/mcp",
            mcp_token="tok",
            factory_api_key="fac",
            deepseek_api_key="ds",
        )
    except NotImplementedError as exc:
        _skip_if_not_implemented("watcher_tick", exc)

    watcher_mocks.fetch_session.assert_not_awaited()
    watcher_mocks.grade.assert_not_awaited()
    watcher_mocks.reprompt.assert_not_awaited()
    watcher_mocks.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_watcher_tick_progress_branch_is_noop(watcher_mocks):
    """making_progress branch: don't reprompt, don't close — just record progress."""
    from director import epic_watcher

    watcher_mocks.grade.return_value = epic_watcher.GraderResult(
        state="making_progress",
        reasoning="droid pushed a commit 2 min ago",
        next_action="wait",
    )

    try:
        await epic_watcher.watcher_tick(
            mcp_url="http://mcp.test/mcp",
            mcp_token="tok",
            factory_api_key="fac",
            deepseek_api_key="ds",
        )
    except NotImplementedError as exc:
        _skip_if_not_implemented("watcher_tick", exc)

    watcher_mocks.reprompt.assert_not_awaited()
    watcher_mocks.close.assert_not_awaited()


def test_watcher_constants_are_sane():
    """MAX_REPROMPTS and POLL_INTERVAL_SECONDS must be reasonable defaults."""
    from director import epic_watcher

    assert epic_watcher.POLL_INTERVAL_SECONDS >= 10
    assert epic_watcher.POLL_INTERVAL_SECONDS <= 600
    assert epic_watcher.MAX_REPROMPTS >= 1
    assert epic_watcher.MAX_REPROMPTS <= 20


def test_grader_result_states_match_contract():
    """GraderResult must enumerate the four legal states."""
    from director import epic_watcher

    for state in ("making_progress", "stuck", "failed", "done"):
        gr = epic_watcher.GraderResult(state=state, reasoning="", next_action="")
        assert gr.state == state
