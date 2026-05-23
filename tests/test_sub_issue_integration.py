"""Integration tests for `director.sub_issue.file_sub_issue` routing.

Worker E owns the implementation. These tests cover the four supported
`kind` values: capability_request, cross_repo, blocked, decision_needed.

External services are mocked: GitHub REST, the MCP server (for the
parent-epic comment and capability requests), and any HTTP plumbing.
When the stub still raises NotImplementedError each test skips with a
clear marker — re-run after Worker E merges.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

# asyncio mark applied per-test below (non-async tests must not carry it).


def _skip_if_not_implemented(name: str, exc: NotImplementedError) -> None:
    pytest.skip(f"{name} stub not implemented yet: {exc}")


@pytest.fixture
def sub_issue_mocks(monkeypatch):
    """Patch the helper points used by file_sub_issue. The wiring assumes
    the implementation calls the module-level helpers
    `_create_github_issue` and `_post_parent_comment`. Workers that pick
    different names should reroute via these mocks.
    """
    from director import sub_issue

    create_gh = AsyncMock(
        return_value={
            "ok": True,
            "url": "https://github.com/lkmotto/motto-mcp-server/issues/123",
            "number": 123,
        }
    )
    post_comment = AsyncMock(return_value=None)

    monkeypatch.setattr(sub_issue, "_create_github_issue", create_gh)
    monkeypatch.setattr(sub_issue, "_post_parent_comment", post_comment)

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("MOTTO_MCP_URL", "http://mcp.test/mcp")
    monkeypatch.setenv("MOTTO_MCP_AUTH_TOKEN", "tok")

    class _NS:
        pass

    ns = _NS()
    ns.create_gh = create_gh
    ns.post_comment = post_comment
    return ns


@pytest.mark.parametrize(
    "kind",
    ["capability_request", "cross_repo", "blocked", "decision_needed"],
)
@pytest.mark.asyncio
async def test_file_sub_issue_creates_github_issue_with_kind_label(
    sub_issue_mocks, kind
):
    """Every supported kind must create a GH issue tagged with the
    canonical labels: 'sub-issue' + 'kind:{kind}'."""
    from director import sub_issue

    try:
        result = await sub_issue.file_sub_issue(
            parent_epic_id=42,
            title=f"sub for {kind}",
            body="body",
            kind=kind,
            repo="lkmotto/motto-mcp-server",
        )
    except NotImplementedError as exc:
        _skip_if_not_implemented(f"file_sub_issue[{kind}]", exc)

    assert result.get("ok") is True
    assert result.get("kind") == kind
    assert result.get("parent_epic_id") == 42
    assert result.get("sub_issue_number") == 123
    assert "sub_issue_url" in result

    sub_issue_mocks.create_gh.assert_awaited_once()
    call_kwargs = sub_issue_mocks.create_gh.await_args.kwargs
    labels = call_kwargs.get("labels") or []
    if not labels and len(sub_issue_mocks.create_gh.await_args.args) >= 4:
        labels = sub_issue_mocks.create_gh.await_args.args[3]
    assert "sub-issue" in labels
    assert f"kind:{kind}" in labels


@pytest.mark.asyncio
async def test_file_sub_issue_capability_request_files_via_mcp(
    monkeypatch, sub_issue_mocks
):
    """The capability_request kind must additionally hit the MCP
    request_capability tool so the cockpit sees the request."""
    from director import sub_issue

    request_cap = AsyncMock(return_value={"id": 5, "status": "pending"})
    monkeypatch.setattr(
        sub_issue, "_request_capability", request_cap, raising=False
    )

    try:
        result = await sub_issue.file_sub_issue(
            parent_epic_id=42,
            title="Need POSTMARK_API_KEY",
            body="Worker D needs the key to send the AMC follow-up.",
            kind="capability_request",
            repo="lkmotto/motto-sdr-agent",
        )
    except NotImplementedError as exc:
        _skip_if_not_implemented("file_sub_issue[capability_request]", exc)

    assert result.get("kind") == "capability_request"
    # Implementations may either call the local _request_capability helper
    # or invoke the MCP tool directly via httpx — both are acceptable.
    if request_cap.await_count == 0:
        # Still ok: integration must have happened SOMEHOW, evidenced by
        # the result having an MCP-backed request_id when wired that way.
        assert result.get("request_id") or result.get("capability_request_id"), (
            "capability_request kind must surface a capability request id"
        )


async def test_file_sub_issue_invalid_kind_raises():
    """Any kind outside the supported set must raise ValueError eagerly,
    before any external call is made."""
    from director import sub_issue

    with pytest.raises(ValueError):
        await sub_issue.file_sub_issue(
            parent_epic_id=1,
            title="bad",
            body="bad",
            kind="not_a_real_kind",
            repo="lkmotto/x",
        )


@pytest.mark.asyncio
async def test_file_sub_issue_prepends_parent_link_to_body(sub_issue_mocks):
    """The body sent to GitHub must reference the parent epic so a human
    landing on the sub-issue can navigate back."""
    from director import sub_issue

    try:
        await sub_issue.file_sub_issue(
            parent_epic_id=42,
            title="t",
            body="original body",
            kind="blocked",
            repo="lkmotto/motto-mcp-server",
        )
    except NotImplementedError as exc:
        _skip_if_not_implemented("file_sub_issue[blocked]", exc)

    call_kwargs = sub_issue_mocks.create_gh.await_args.kwargs
    body_text = call_kwargs.get("body") or ""
    if not body_text and len(sub_issue_mocks.create_gh.await_args.args) >= 3:
        body_text = sub_issue_mocks.create_gh.await_args.args[2]
    assert "42" in body_text, "parent epic id must appear in the sub-issue body"


@pytest.mark.asyncio
async def test_file_sub_issue_posts_parent_comment(sub_issue_mocks):
    """After creating the sub-issue, the implementation must post a
    cross-link comment on the parent epic via MCP."""
    from director import sub_issue

    try:
        await sub_issue.file_sub_issue(
            parent_epic_id=42,
            title="t",
            body="b",
            kind="decision_needed",
            repo="lkmotto/motto-mcp-server",
        )
    except NotImplementedError as exc:
        _skip_if_not_implemented("file_sub_issue[decision_needed]", exc)

    sub_issue_mocks.post_comment.assert_awaited()
    args = sub_issue_mocks.post_comment.await_args
    kw = args.kwargs
    assert (
        kw.get("parent_epic_id") == 42
        or (len(args.args) >= 1 and args.args[0] == 42)
    )
    sub_url = kw.get("sub_issue_url") or (
        args.args[1] if len(args.args) >= 2 else ""
    )
    assert "github.com" in sub_url
