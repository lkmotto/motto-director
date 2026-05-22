"""Tests for compound-PR handling in act + compound modules."""

from __future__ import annotations

import json

import httpx
import respx

from director.act import act
from director.compound import (
    DEFAULT_COMPOUND_BRANCH,
    SELF_REPO,
    is_self_mod,
    parse_pr_entries,
)
from director.ideate import NextMove
from director.perceive import Snapshot

REPO = "lkmotto/motto-social-agent"
GH = "https://api.github.com"


def _move(
    *, code_changes: list[dict[str, str]] | None = None, title: str = "Tighten retry"
) -> NextMove:
    return NextMove(
        repo=REPO,
        kind="compound_pr",
        title=title,
        rationale="reduce flakiness",
        prompt_for_claude_code="",
        priority=2,
        intent=(
            "Cron run NN observed 3 transient failures; small retry tweak unblocks the next cycle."
        ),
        code_changes=code_changes or [{"path": "src/retry.py", "content": "TIMEOUT = 10\n"}],
    )


def _empty_snapshot() -> Snapshot:
    return Snapshot(captured_at="2026-04-27T00:00:00Z", repos=[])


def _mock_repo_meta(mock: respx.MockRouter, *, default_branch: str = "main") -> None:
    mock.get(f"{GH}/repos/{REPO}").mock(
        return_value=httpx.Response(200, json={"default_branch": default_branch})
    )
    mock.get(f"{GH}/repos/{REPO}/git/ref/heads/{default_branch}").mock(
        return_value=httpx.Response(200, json={"object": {"sha": "main-sha-1234567"}})
    )


def _mock_branch_missing(mock: respx.MockRouter) -> None:
    mock.get(f"{GH}/repos/{REPO}/git/ref/heads/{DEFAULT_COMPOUND_BRANCH}").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    mock.post(f"{GH}/repos/{REPO}/git/refs").mock(
        return_value=httpx.Response(
            201,
            json={"ref": f"refs/heads/{DEFAULT_COMPOUND_BRANCH}", "object": {"sha": "x"}},
        )
    )


def _mock_pr_open(
    mock: respx.MockRouter,
    *,
    pr_number: int = 17,
    body: str = "",
) -> dict:
    pr = {
        "number": pr_number,
        "node_id": f"PR_node_{pr_number}",
        "html_url": f"https://github.com/{REPO}/pull/{pr_number}",
        "body": body,
    }
    mock.get(f"{GH}/repos/{REPO}/pulls").mock(return_value=httpx.Response(200, json=[pr]))
    return pr


def _mock_contents_create_then_update(mock: respx.MockRouter, path: str) -> None:
    mock.get(f"{GH}/repos/{REPO}/contents/{path}").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    mock.put(f"{GH}/repos/{REPO}/contents/{path}").mock(
        return_value=httpx.Response(
            201,
            json={"commit": {"sha": "commit-sha-abcdef0"}},
        )
    )


def _mock_pr_patch(mock: respx.MockRouter, pr_number: int = 17) -> respx.Route:
    return mock.patch(f"{GH}/repos/{REPO}/pulls/{pr_number}").mock(
        return_value=httpx.Response(200, json={"number": pr_number})
    )


def test_compound_append_twice_accumulates_entries(monkeypatch):
    """Two ticks each appending one move → PR description carries both entries."""
    monkeypatch.setenv("GITHUB_TOKEN", "test")
    monkeypatch.delenv("DIRECTOR_AUTO_MERGE", raising=False)
    monkeypatch.setenv("DIRECTOR_COMPOUND_MAX_MOVES", "10")

    with respx.mock(assert_all_called=False) as mock:
        _mock_repo_meta(mock)
        # Tick 1: branch missing → create.
        _mock_branch_missing(mock)
        # PR initially open with empty body. We'll capture PATCH calls to read
        # back the rewritten body.
        pr_state = {"body": ""}
        mock.get(f"{GH}/repos/{REPO}/pulls").mock(
            side_effect=lambda req: httpx.Response(
                200,
                json=[
                    {
                        "number": 17,
                        "node_id": "PR_node_17",
                        "html_url": "x",
                        "body": pr_state["body"],
                    }
                ],
            )
        )
        _mock_contents_create_then_update(mock, "src/retry.py")

        def _capture_patch(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode())
            pr_state["body"] = payload["body"]
            return httpx.Response(200, json={"number": 17})

        mock.patch(f"{GH}/repos/{REPO}/pulls/17").mock(side_effect=_capture_patch)

        # Tick 1
        results1 = act([_move()], _empty_snapshot())
        assert results1[0].status == "executed"
        entries_after_tick1 = parse_pr_entries(pr_state["body"])
        assert len(entries_after_tick1) == 1
        assert entries_after_tick1[0].title == "Tighten retry"

        # Tick 2: branch now exists, PR body carries the prior entry.
        mock.get(f"{GH}/repos/{REPO}/git/ref/heads/{DEFAULT_COMPOUND_BRANCH}").mock(
            return_value=httpx.Response(200, json={"object": {"sha": "branch-head"}})
        )

        results2 = act([_move(title="Bump pyarrow")], _empty_snapshot())
        assert results2[0].status == "executed"
        entries_after_tick2 = parse_pr_entries(pr_state["body"])
        assert [e.title for e in entries_after_tick2] == [
            "Tighten retry",
            "Bump pyarrow",
        ]


def test_compound_max_moves_flushes_via_auto_merge(monkeypatch, capsys):
    """When entries reach DIRECTOR_COMPOUND_MAX_MOVES, auto-merge is enabled
    even if DIRECTOR_AUTO_MERGE is unset."""
    monkeypatch.setenv("GITHUB_TOKEN", "test")
    monkeypatch.delenv("DIRECTOR_AUTO_MERGE", raising=False)
    monkeypatch.setenv("DIRECTOR_COMPOUND_MAX_MOVES", "2")

    # Pre-seed PR body with one prior entry; this tick adds the second → triggers flush.
    seeded_body = (
        "header\n<!-- director-compound-state -->\n```json\n"
        + json.dumps(
            [
                {
                    "ts": "2026-04-26T00:00:00+00:00",
                    "move_kind": "compound_pr",
                    "title": "earlier",
                    "rationale": "r",
                    "commit_sha": "abc1234",
                }
            ]
        )
        + "\n```\n<!-- /director-compound-state -->\n"
    )

    with respx.mock(assert_all_called=False) as mock:
        _mock_repo_meta(mock)
        mock.get(f"{GH}/repos/{REPO}/git/ref/heads/{DEFAULT_COMPOUND_BRANCH}").mock(
            return_value=httpx.Response(200, json={"object": {"sha": "x"}})
        )
        _mock_pr_open(mock, body=seeded_body)
        _mock_contents_create_then_update(mock, "src/retry.py")
        _mock_pr_patch(mock)

        graphql_route = mock.post("https://api.github.com/graphql").mock(
            return_value=httpx.Response(
                200, json={"data": {"enablePullRequestAutoMerge": {"pullRequest": {"number": 17}}}}
            )
        )

        results = act([_move()], _empty_snapshot())

    assert results[0].status == "executed"
    assert graphql_route.call_count == 1
    log_events = [
        json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip().startswith("{")
    ]
    flushed = [e for e in log_events if e["event"] == "director.compound_flushed"]
    assert flushed and "max_moves_reached" in flushed[0]["reason"]
    enabled = [e for e in log_events if e["event"] == "director.auto_merge_enabled"]
    assert len(enabled) == 1


def test_compound_auto_merge_gate_respects_dry_run(monkeypatch):
    """In DRY_RUN, no GitHub calls happen at all — including no auto-merge."""
    monkeypatch.setenv("DIRECTOR_DRY_RUN", "1")
    monkeypatch.setenv("DIRECTOR_AUTO_MERGE", "true")

    with respx.mock(assert_all_called=False) as mock:
        gh_route = mock.route(host="api.github.com").mock(return_value=httpx.Response(500))
        results = act([_move()], _empty_snapshot())

    assert results[0].status == "dry_run"
    assert gh_route.call_count == 0


def test_self_mod_paths_are_blocked_without_opt_in(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test")
    monkeypatch.delenv("DIRECTOR_ALLOW_SELF_MOD", raising=False)

    move = NextMove(
        repo=SELF_REPO,
        kind="compound_pr",
        title="self-mod",
        rationale="x",
        prompt_for_claude_code="",
        priority=3,
        intent="Tightening own perceive logic based on cron logs.",
        code_changes=[{"path": "director/perceive.py", "content": "# pwn\n"}],
    )

    with respx.mock(assert_all_called=False) as mock:
        gh_route = mock.route(host="api.github.com").mock(return_value=httpx.Response(500))
        results = act([move], _empty_snapshot())

    assert results[0].status == "skipped"
    assert "self-modifying" in results[0].detail
    assert gh_route.call_count == 0


def test_is_self_mod_only_triggers_for_self_repo():
    assert is_self_mod(SELF_REPO, ["director/perceive.py"]) is True
    assert is_self_mod(SELF_REPO, ["README.md"]) is False
    assert is_self_mod("lkmotto/motto-social-agent", ["director/perceive.py"]) is False
