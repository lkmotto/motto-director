"""Tests for director.meta — gather_outcomes, synthesize_improvements, file_meta_pr."""

from __future__ import annotations

import asyncio

import httpx
import respx

from director import meta
from director.meta import Improvement


def _decision_event(choice: str, evidence: dict) -> dict:
    return {
        "kind": "decision",
        "level": "info",
        "payload": {"choice": choice, "evidence": evidence},
    }


# ── gather_outcomes ──────────────────────────────────────────────────────────


def test_gather_outcomes_buckets_sessions(monkeypatch):
    spawn_evidence_merged = {
        "repo": "lkmotto/motto-sdr-agent",
        "title": "Add retry test",
    }
    spawn_evidence_closed = {
        "repo": "lkmotto/motto-sdr-agent",
        "title": "Refactor pipeline",
    }
    spawn_evidence_open = {
        "repo": "lkmotto/motto-sdr-agent",
        "title": "Wire feature flag",
    }
    spawn_evidence_unmatched = {
        "repo": "lkmotto/motto-sdr-agent",
        "title": "Some title that doesn't exist",
    }
    fake_events = [
        _decision_event("spawned_claude_session", spawn_evidence_merged),
        _decision_event("spawned_claude_session", spawn_evidence_closed),
        _decision_event("spawned_claude_session", spawn_evidence_open),
        _decision_event("spawned_claude_session", spawn_evidence_unmatched),
        _decision_event(
            "merged_pr",
            {"repo": "lkmotto/motto-social-agent", "pr_number": 99},
        ),
        # Noise events (non-decision) should be ignored.
        {"kind": "acted", "payload": {}, "level": "info"},
        # Failure-pattern events seen 2+ times.
        {"kind": "ev", "level": "warn", "payload": {"detail": "ruff format failed"}},
        {"kind": "ev", "level": "warn", "payload": {"detail": "ruff format issue"}},
    ]

    async def fake_recent_events(**_kwargs):
        return fake_events

    monkeypatch.setattr(meta.fleet, "recent_events", fake_recent_events)
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

    fake_prs = [
        {
            "number": 1,
            "title": "Add retry test",
            "state": "closed",
            "merged_at": "2026-05-01T00:00:00Z",
            "additions": 10,
            "deletions": 2,
            "changed_files": 1,
        },
        {
            "number": 2,
            "title": "Refactor pipeline",
            "state": "closed",
            "merged_at": None,
            "additions": 50,
            "deletions": 30,
            "changed_files": 5,
        },
        {
            "number": 3,
            "title": "Wire feature flag",
            "state": "open",
            "merged_at": None,
            "additions": 5,
            "deletions": 1,
            "changed_files": 1,
        },
    ]

    repo = "lkmotto/motto-sdr-agent"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"https://api.github.com/repos/{repo}/pulls").mock(
            return_value=httpx.Response(200, json=fake_prs)
        )
        outcomes = asyncio.run(meta.gather_outcomes(since_days=7))

    assert outcomes["sessions_spawned"] == 4
    assert outcomes["sessions_succeeded"] == 1
    # closed-unmerged + unmatched (treated as fail since we couldn't trace it).
    assert outcomes["sessions_failed"] == 2
    assert len(outcomes["prs_merged"]) == 2  # 1 from spawn + 1 from merge_pr
    assert len(outcomes["prs_closed_unmerged"]) == 1
    assert "ruff format" in outcomes["common_failure_patterns"]


def test_gather_outcomes_empty_when_no_fleet(monkeypatch):
    async def fake_recent_events(**_kwargs):
        return []

    monkeypatch.setattr(meta.fleet, "recent_events", fake_recent_events)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    outcomes = asyncio.run(meta.gather_outcomes(since_days=7))
    assert outcomes["sessions_spawned"] == 0
    assert outcomes["sessions_succeeded"] == 0
    assert outcomes["sessions_failed"] == 0
    assert outcomes["prs_merged"] == []
    assert outcomes["common_failure_patterns"] == []


# ── synthesize_improvements ──────────────────────────────────────────────────


def test_synthesize_filters_low_confidence_and_caps_at_three(monkeypatch):
    fake_response = {
        "improvements": [
            {
                "target_file": "director/policy.py",
                "current_excerpt": "MAX_FILES_PER_SESSION = 3",
                "proposed_change": "MAX_FILES_PER_SESSION = 4",
                "rationale": "Successful sessions averaged 3.6 files; bump to allow.",
                "confidence": 0.9,
            },
            {
                "target_file": "skills/spawn-session.md",
                "current_excerpt": "...",
                "proposed_change": "Add line: 'Always run ruff format after edits.'",
                "rationale": "ruff format failed on 5/12 sessions.",
                "confidence": 0.85,
            },
            {
                "target_file": "director/policy.py",
                "current_excerpt": "TIER_1_PATTERNS = (...)",
                "proposed_change": "Add 'CHANGELOG.md' to TIER_1_PATTERNS.",
                "rationale": "CHANGELOG-only PRs blocked unnecessarily.",
                "confidence": 0.78,
            },
            {
                # Filtered: low confidence.
                "target_file": "director/policy.py",
                "current_excerpt": "x",
                "proposed_change": "y",
                "rationale": "shaky",
                "confidence": 0.3,
            },
            {
                # Filtered: not in allowed target files.
                "target_file": "director/act.py",
                "current_excerpt": "x",
                "proposed_change": "y",
                "rationale": "out of scope",
                "confidence": 0.95,
            },
            {
                # Would be the 4th high-confidence — capped out by MAX_IMPROVEMENTS_PER_RUN.
                "target_file": "skills/something.md",
                "current_excerpt": "x",
                "proposed_change": "y",
                "rationale": "extra",
                "confidence": 0.75,
            },
        ]
    }

    import json as _json

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake")
    monkeypatch.setattr(
        meta,
        "_call_llm_chain",
        lambda system, user_msg: f"```json\n{_json.dumps(fake_response)}\n```",
    )

    improvements = asyncio.run(meta.synthesize_improvements({"sessions_spawned": 12}))
    assert len(improvements) == 3
    assert all(i.confidence >= 0.7 for i in improvements)
    # Highest confidence first.
    assert improvements[0].confidence >= improvements[1].confidence
    # Out-of-scope target was filtered.
    assert all(
        i.target_file == "director/policy.py" or i.target_file.startswith("skills/")
        for i in improvements
    )


def test_synthesize_returns_empty_when_no_llm_keys(monkeypatch):
    for key in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    out = asyncio.run(meta.synthesize_improvements({"sessions_spawned": 5}))
    assert out == []


def test_any_llm_provider_recognizes_claude_max(monkeypatch):
    """Regression: after PR #24, CLAUDE_CODE_OAUTH_TOKEN became the deployed
    primary provider. Prior to this fix, _any_llm_provider_configured()
    only checked the four pre-PR-#24 keys, so the entire weekly meta cron
    silently no-opped whenever claude_max was the only configured provider."""
    for key in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    assert meta._any_llm_provider_configured() is False
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-oauth-token")
    assert meta._any_llm_provider_configured() is True


def test_synthesize_handles_malformed_llm_response(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake")
    monkeypatch.setattr(meta, "_call_llm_chain", lambda s, u: "not json at all")
    out = asyncio.run(meta.synthesize_improvements({"sessions_spawned": 0}))
    assert out == []


# ── file_meta_pr ─────────────────────────────────────────────────────────────


def _imp(
    target: str = "director/policy.py",
    confidence: float = 0.85,
) -> Improvement:
    return Improvement(
        target_file=target,
        current_excerpt="MAX_FILES_PER_SESSION = 3",
        proposed_change="MAX_FILES_PER_SESSION = 4",
        rationale="Bump cap based on weekly outcomes.",
        confidence=confidence,
    )


def test_file_meta_pr_returns_none_when_no_improvements(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    out = asyncio.run(meta.file_meta_pr([]))
    assert out is None


def test_file_meta_pr_returns_none_when_no_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    out = asyncio.run(meta.file_meta_pr([_imp()]))
    assert out is None


def test_file_meta_pr_files_pr_with_director_meta_label_only(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-tok")
    repo = meta.SELF_REPO
    GH = "https://api.github.com"

    captured: dict[str, object] = {}

    def _capture_post_pulls(request: httpx.Request) -> httpx.Response:
        captured["pr_body"] = __import__("json").loads(request.content.decode())
        return httpx.Response(
            201,
            json={
                "number": 42,
                "node_id": "PR_42",
                "html_url": f"https://github.com/{repo}/pull/42",
            },
        )

    def _capture_labels(request: httpx.Request) -> httpx.Response:
        captured["labels_body"] = __import__("json").loads(request.content.decode())
        return httpx.Response(200, json=[{"name": meta.META_LABEL}])

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{GH}/repos/{repo}").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        mock.get(f"{GH}/repos/{repo}/git/ref/heads/main").mock(
            return_value=httpx.Response(200, json={"object": {"sha": "main-sha-123"}})
        )
        mock.post(f"{GH}/repos/{repo}/git/refs").mock(
            return_value=httpx.Response(201, json={"ref": "x", "object": {"sha": "y"}})
        )
        mock.put(host="api.github.com", path__regex=r"/repos/.+/contents/.+").mock(
            return_value=httpx.Response(201, json={"commit": {"sha": "z"}})
        )
        mock.post(f"{GH}/repos/{repo}/pulls").mock(side_effect=_capture_post_pulls)
        mock.post(f"{GH}/repos/{repo}/labels").mock(
            return_value=httpx.Response(422, json={"message": "already_exists"})
        )
        mock.post(f"{GH}/repos/{repo}/issues/42/labels").mock(side_effect=_capture_labels)

        url = asyncio.run(meta.file_meta_pr([_imp(), _imp(target="skills/foo.md")]))

    assert url == f"https://github.com/{repo}/pull/42"
    pr_body = captured["pr_body"]
    assert pr_body["title"].startswith("Director-meta:")
    assert pr_body["draft"] is True
    # Body must mention each improvement and call out non-auto-merge.
    assert "auto-merge-ok" in pr_body["body"]
    assert "advisory" in pr_body["body"].lower()
    assert pr_body["base"] == "main"

    labels_body = captured["labels_body"]
    assert labels_body == {"labels": [meta.META_LABEL]}
    # Hard constraint: never apply auto-merge-ok from this code path.
    assert "auto-merge-ok" not in labels_body["labels"]


# ── helper coverage ──────────────────────────────────────────────────────────


def test_target_file_allowed_whitelist():
    assert meta._target_file_allowed("director/policy.py")
    assert meta._target_file_allowed("skills/spawn-session.md")
    assert not meta._target_file_allowed("director/act.py")
    assert not meta._target_file_allowed("skills/spawn-session.txt")
    assert not meta._target_file_allowed("../etc/passwd")


def test_match_pr_by_title_substring():
    prs = [
        {"title": "feat: Add retry test"},
        {"title": "Wire feature flag"},
    ]
    assert meta._match_pr_by_title(prs, "Add retry test") is not None
    assert meta._match_pr_by_title(prs, "Wire feature flag") is not None
    assert meta._match_pr_by_title(prs, "totally unrelated") is None
    assert meta._match_pr_by_title(prs, "") is None


def test_classify_pr_buckets():
    assert meta._classify_pr({"merged_at": "2026-05-01T00:00:00Z"}) == "merged"
    assert meta._classify_pr({"merged_at": None, "state": "closed"}) == "closed"
    assert meta._classify_pr({"merged_at": None, "state": "open"}) == "open"
