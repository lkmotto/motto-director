"""Phase 5.7: morning Telegram digest.

Run by a separate Northflank cron at 7am CT (12pm UTC). Reads recent fleet
runs/events, walks the current GitHub snapshot, and sends a single short
markdown summary to Luke's Telegram so he sees overnight progress at a glance.

No-ops gracefully when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are unset.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, date, datetime
from typing import Any

import httpx

from director import fleet
from director.perceive import Snapshot, perceive

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

# Approx token cost in $ per 1k input/output tokens, used for the burn line.
# Tuned to motto-director's primary provider (deepseek-chat); off by ~3x
# for anthropic fallback runs but those are rare so it's a directional
# number, not a billing source.
APPROX_COST_PER_RUN_USD = 0.01


def _log(event_name: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event_name,
        **fields,
    }
    print(json.dumps(record, default=str), flush=True)


def _md_v2_escape(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters in user-supplied content.

    All of `_*[]()~` `>#+-=|{}.!\\` must be escaped or Telegram rejects the
    message. Only call this on raw text — not on already-formatted markdown.
    """
    specials = r"_*[]()~`>#+-=|{}.!\\"
    out = []
    for ch in text:
        if ch in specials:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _link(label: str, url: str) -> str:
    """MarkdownV2 inline link with both label and url escaped."""
    safe_label = _md_v2_escape(label)
    safe_url = url.replace(")", r"\)").replace("\\", r"\\")
    return f"[{safe_label}]({safe_url})"


async def build_digest(window_hours: int = 24) -> str:
    """Compose the morning digest markdown. Pure-build — caller sends it.

    Reads:
    - fleet runs in the last `window_hours` (via MCP list_runs)
    - fleet events in the same window for `acted` and `decision` kinds
    - the current GitHub snapshot via perceive()

    Returns a MarkdownV2-escaped string that's safe to send via Telegram.
    """
    since_minutes = window_hours * 60
    runs = await fleet.list_runs(
        agent_name="motto-director",
        since_minutes=since_minutes,
        limit=200,
    )
    events = await fleet.recent_events(
        since_minutes=since_minutes,
        agent_name="motto-director",
        limit=1000,
    )

    decisions = [e for e in events if e.get("kind") == "decision"]
    merged = [d for d in decisions if (d.get("payload") or {}).get("choice") == "merged_pr"]
    sessions = [
        d for d in decisions if (d.get("payload") or {}).get("choice") == "spawned_claude_session"
    ]
    sessions_succeeded = [
        s for s in sessions if ((s.get("payload") or {}).get("evidence") or {}).get("session_url")
    ]
    sessions_failed = len(sessions) - len(sessions_succeeded)

    snapshot = perceive()
    awaiting_review, needs_judgment = _classify_open_prs(snapshot)

    total_events = len(events)
    total_runs = len(runs)
    est_cost = total_runs * APPROX_COST_PER_RUN_USD

    return _render(
        window_hours=window_hours,
        merged=merged,
        awaiting_review=awaiting_review,
        sessions_total=len(sessions),
        sessions_succeeded=len(sessions_succeeded),
        sessions_failed=sessions_failed,
        needs_judgment=needs_judgment,
        total_runs=total_runs,
        total_events=total_events,
        est_cost=est_cost,
    )


def _classify_open_prs(
    snapshot: Snapshot,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split open PRs into 'awaiting your review' and 'needs your judgment'.

    awaiting_review: CI green, no decision yet (review_state == 'pending').
    needs_judgment: CI failed, conflict, or stuck — flagged with a one-liner.
    """
    awaiting: list[dict[str, Any]] = []
    needs: list[dict[str, Any]] = []
    for repo in snapshot.repos:
        for pr in repo.open_prs:
            entry = {
                "repo": pr.repo,
                "number": pr.number,
                "title": pr.title,
                "url": pr.url,
                "ci_status": pr.ci_status,
                "review_state": pr.review_state,
                "age_hours": pr.age_hours,
            }
            if pr.ci_status == "failure":
                entry["reason"] = "failed CI"
                needs.append(entry)
            elif pr.review_state == "changes_requested":
                entry["reason"] = "changes requested"
                needs.append(entry)
            elif pr.ci_status == "success" and pr.review_state == "pending":
                awaiting.append(entry)
            elif pr.age_hours > 48 and pr.review_state == "pending":
                entry["reason"] = f"stalled {int(pr.age_hours)}h with no decision"
                needs.append(entry)
    return awaiting, needs


def _render(
    *,
    window_hours: int,
    merged: list[dict[str, Any]],
    awaiting_review: list[dict[str, Any]],
    sessions_total: int,
    sessions_succeeded: int,
    sessions_failed: int,
    needs_judgment: list[dict[str, Any]],
    total_runs: int,
    total_events: int,
    est_cost: float,
) -> str:
    today = date.today().isoformat()
    lines: list[str] = []
    lines.append(f"🌅 *Director morning digest* — {_md_v2_escape(today)}")
    lines.append("")
    lines.append(f"📦 *Last {window_hours}h:*")

    if merged:
        merged_links = ", ".join(
            _link(
                f"{(d.get('payload') or {}).get('evidence', {}).get('repo', '?')}"
                f"#{(d.get('payload') or {}).get('evidence', {}).get('pr_number', '?')}",
                (d.get("payload") or {}).get("evidence", {}).get("url", ""),
            )
            for d in merged
            if (d.get("payload") or {}).get("evidence", {}).get("url")
        )
        lines.append(f"• {len(merged)} PRs merged: {merged_links or '_unlinked_'}")
    else:
        lines.append("• 0 PRs merged")

    if awaiting_review:
        review_links = ", ".join(
            _link(f"{p['repo'].split('/')[-1]}#{p['number']}", p["url"])
            for p in awaiting_review[:8]
        )
        more = "" if len(awaiting_review) <= 8 else f" \\(+{len(awaiting_review) - 8} more\\)"
        lines.append(f"• {len(awaiting_review)} PRs awaiting your review: {review_links}{more}")
    else:
        lines.append("• 0 PRs awaiting your review")

    lines.append(
        f"• {sessions_total} Claude Code sessions spawned "
        f"\\({sessions_succeeded} succeeded, {sessions_failed} failed\\)"
    )
    lines.append("")

    if needs_judgment:
        lines.append("⚠️ *Needs your judgment:*")
        for p in needs_judgment[:10]:
            label = f"{p['repo'].split('/')[-1]}#{p['number']}"
            reason = _md_v2_escape(p.get("reason", "review"))
            lines.append(f"• {_link(label, p['url'])}: {reason}")
        if len(needs_judgment) > 10:
            lines.append(f"• \\(+{len(needs_judgment) - 10} more\\)")
        lines.append("")

    cost_str = _md_v2_escape(f"${est_cost:.2f}")
    lines.append(
        f"💸 *Burn \\(last {window_hours}h\\):* "
        f"{total_runs} runs, {total_events} events, est cost: {cost_str}"
    )
    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    """Send `text` to Luke's Telegram. Returns True on success.

    No-ops + returns False when TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is
    unset — the digest cron is optional infrastructure.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        _log("digest.skipped", reason="telegram env unset")
        return False
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                f"{TELEGRAM_API}/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                    "disable_web_page_preview": True,
                },
            )
        if r.status_code >= 300:
            _log(
                "digest.telegram_error",
                status_code=r.status_code,
                body=r.text[:500],
            )
            return False
        return True
    except httpx.HTTPError as exc:
        _log("digest.telegram_error", error=str(exc))
        return False


async def _build_and_send_async(window_hours: int) -> int:
    text = await build_digest(window_hours=window_hours)
    _log("digest.built", chars=len(text))
    sent = send_telegram(text)
    _log("digest.done", sent=sent)
    return 0 if sent else 1


def run_digest() -> int:
    """Sync entry point for the `motto-director-digest` console script."""
    window_hours = int(os.environ.get("DIGEST_WINDOW_HOURS", "24"))
    return asyncio.run(_build_and_send_async(window_hours))


if __name__ == "__main__":
    sys.exit(run_digest())
