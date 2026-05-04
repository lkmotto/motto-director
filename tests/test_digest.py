"""Tests for director.digest — markdown rendering + Telegram sender no-op."""

from __future__ import annotations

import asyncio

import httpx
import respx

from director import digest
from director.perceive import PullRequest, RepoState, Snapshot


def _empty_snapshot() -> Snapshot:
    return Snapshot(captured_at="2026-05-04T00:00:00Z", repos=[])


def _snapshot_with(prs: list[PullRequest]) -> Snapshot:
    by_repo: dict[str, list[PullRequest]] = {}
    for pr in prs:
        by_repo.setdefault(pr.repo, []).append(pr)
    return Snapshot(
        captured_at="2026-05-04T00:00:00Z",
        repos=[
            RepoState(repo=r, open_prs=p, open_issues=[], default_branch="main")
            for r, p in by_repo.items()
        ],
    )


def _pr(
    *,
    repo: str = "lkmotto/motto-sdr-agent",
    number: int = 1,
    title: str = "x",
    ci_status: str = "success",
    review_state: str = "pending",
    approvals: int = 0,
    labels: tuple[str, ...] = (),
    age_hours: float = 4.0,
) -> PullRequest:
    return PullRequest(
        repo=repo,
        number=number,
        title=title,
        url=f"https://github.com/{repo}/pull/{number}",
        age_hours=age_hours,
        ci_status=ci_status,
        review_state=review_state,
        approvals=approvals,
        labels=list(labels),
        head_sha="aaa",
    )


def test_send_telegram_returns_false_when_env_unset(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert digest.send_telegram("hi") is False


def test_send_telegram_returns_false_when_chat_id_missing(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert digest.send_telegram("hi") is False


def test_send_telegram_posts_to_bot_api_when_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("https://api.telegram.org/botabc:tok/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        ok = digest.send_telegram("hi")
    assert ok is True
    assert route.call_count == 1


def test_send_telegram_returns_false_on_api_error(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://api.telegram.org/botabc:tok/sendMessage").mock(
            return_value=httpx.Response(400, json={"ok": False, "description": "bad"})
        )
        assert digest.send_telegram("hi") is False


def test_md_v2_escape_handles_specials():
    assert digest._md_v2_escape("a.b") == r"a\.b"
    assert digest._md_v2_escape("(hi)") == r"\(hi\)"
    assert digest._md_v2_escape("plain") == "plain"


def test_build_digest_renders_all_sections(monkeypatch):
    """Inject canned fleet data + a snapshot with a mix of PR states."""
    fake_runs = [{"id": str(i), "status": "success"} for i in range(7)]
    fake_events = [
        {
            "kind": "decision",
            "payload": {
                "choice": "merged_pr",
                "evidence": {
                    "repo": "lkmotto/motto-sdr-agent",
                    "pr_number": 5,
                    "url": "https://github.com/lkmotto/motto-sdr-agent/pull/5",
                },
            },
        },
        {
            "kind": "decision",
            "payload": {
                "choice": "spawned_claude_session",
                "evidence": {
                    "repo": "lkmotto/motto-social-agent",
                    "session_url": "https://claude.ai/c/sess_1",
                },
            },
        },
        {
            "kind": "decision",
            "payload": {
                "choice": "spawned_claude_session",
                "evidence": {
                    "repo": "lkmotto/motto-social-agent",
                    "session_url": "",  # failed
                },
            },
        },
        {"kind": "acted", "payload": {}},
    ]

    async def fake_list_runs(**_kwargs):
        return fake_runs

    async def fake_recent_events(**_kwargs):
        return fake_events

    snapshot = _snapshot_with(
        [
            _pr(number=1, title="awaiting", ci_status="success", review_state="pending"),
            _pr(
                number=2,
                title="needs",
                ci_status="failure",
                review_state="pending",
            ),
        ]
    )

    monkeypatch.setattr(digest.fleet, "list_runs", fake_list_runs)
    monkeypatch.setattr(digest.fleet, "recent_events", fake_recent_events)
    monkeypatch.setattr(digest, "perceive", lambda: snapshot)

    text = asyncio.run(digest.build_digest(window_hours=24))

    assert "Director morning digest" in text
    assert "Last 24h" in text
    assert "1 PRs merged" in text
    assert "1 PRs awaiting your review" in text
    # 2 sessions: 1 succeeded, 1 failed.
    assert "2 Claude Code sessions spawned" in text
    assert "1 succeeded" in text
    assert "1 failed" in text
    assert "Needs your judgment" in text
    assert "failed CI" in text
    assert "7 runs" in text


def test_build_digest_with_no_data_renders_zeros(monkeypatch):
    async def fake_list_runs(**_kwargs):
        return []

    async def fake_recent_events(**_kwargs):
        return []

    monkeypatch.setattr(digest.fleet, "list_runs", fake_list_runs)
    monkeypatch.setattr(digest.fleet, "recent_events", fake_recent_events)
    monkeypatch.setattr(digest, "perceive", _empty_snapshot)

    text = asyncio.run(digest.build_digest(window_hours=24))
    assert "0 PRs merged" in text
    assert "0 PRs awaiting your review" in text
    assert "0 Claude Code sessions spawned" in text
    # No "Needs your judgment" section when list is empty.
    assert "Needs your judgment" not in text
    assert "0 runs" in text


def test_classify_open_prs_buckets_correctly():
    snapshot = _snapshot_with(
        [
            _pr(number=1, ci_status="success", review_state="pending"),  # awaiting
            _pr(number=2, ci_status="failure", review_state="pending"),  # needs
            _pr(number=3, ci_status="success", review_state="changes_requested"),  # needs
            _pr(number=4, ci_status="success", review_state="approved"),  # neither
            _pr(
                number=5,
                ci_status="success",
                review_state="pending",
                age_hours=80.0,
            ),  # awaiting (caught by success+pending first)
        ]
    )
    awaiting, needs = digest._classify_open_prs(snapshot)
    awaiting_nums = {p["number"] for p in awaiting}
    needs_nums = {p["number"] for p in needs}
    assert 1 in awaiting_nums
    assert 5 in awaiting_nums
    assert 2 in needs_nums
    assert 3 in needs_nums
    assert 4 not in awaiting_nums and 4 not in needs_nums
