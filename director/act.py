"""Act: execute top-N moves (file_issue, spawn_session, merge_pr, nudge_pipeline)."""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from director.ideate import NextMove
from director.perceive import PullRequest, Snapshot

GITHUB_API = "https://api.github.com"
CLAUDE_CODE_SESSIONS_URL = "https://claude.ai/api/sessions"
APPRAISAL_PIPELINE_TICK_URL = os.environ.get(
    "APPRAISAL_PIPELINE_TICK_URL",
    "https://appraisal-pipeline.motto.internal/tick",
)
AUTO_MERGE_LABEL = "auto-merge-ok"


@dataclass
class ActResult:
    move: NextMove
    status: str  # "executed" | "skipped" | "dry_run" | "error"
    detail: str = ""


def _dry_run() -> bool:
    return os.environ.get("DIRECTOR_DRY_RUN", "0") == "1"


def _gh_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _claude_session_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ.get('CLAUDE_CODE_SESSION_TOKEN', '')}",
        "Content-Type": "application/json",
    }


def _find_pr(snapshot: Snapshot, repo: str, title: str) -> PullRequest | None:
    for state in snapshot.repos:
        if state.repo != repo:
            continue
        for pr in state.open_prs:
            if pr.title == title or str(pr.number) in title:
                return pr
    return None


def _file_issue(client: httpx.Client, move: NextMove) -> ActResult:
    body = (
        f"**Intent:** {move.intent}\n\n"
        f"**Rationale:** {move.rationale}\n\n"
        "_Filed by motto-director._"
    )
    r = client.post(
        f"{GITHUB_API}/repos/{move.repo}/issues",
        headers=_gh_headers(),
        json={"title": move.title, "body": body},
    )
    if r.status_code >= 300:
        return ActResult(move=move, status="error", detail=f"{r.status_code} {r.text}")
    return ActResult(move=move, status="executed", detail=r.json().get("html_url", ""))


def _spawn_session(client: httpx.Client, move: NextMove) -> ActResult:
    if not move.prompt_for_claude_code:
        return ActResult(
            move=move, status="skipped", detail="missing prompt_for_claude_code"
        )
    r = client.post(
        CLAUDE_CODE_SESSIONS_URL,
        headers=_claude_session_headers(),
        json={
            "repo": move.repo,
            "prompt": move.prompt_for_claude_code,
            "intent": move.intent,
        },
    )
    if r.status_code >= 300:
        return ActResult(move=move, status="error", detail=f"{r.status_code} {r.text}")
    data = r.json() if r.text else {}
    return ActResult(
        move=move,
        status="executed",
        detail=data.get("session_url") or data.get("id", ""),
    )


def _merge_pr(
    client: httpx.Client, move: NextMove, snapshot: Snapshot
) -> ActResult:
    pr = _find_pr(snapshot, move.repo, move.title)
    if pr is None:
        return ActResult(
            move=move, status="skipped", detail="PR not found in snapshot"
        )
    if pr.ci_status != "success":
        return ActResult(
            move=move, status="skipped", detail=f"CI is {pr.ci_status}"
        )
    if pr.approvals < 1:
        return ActResult(move=move, status="skipped", detail="no approvals")
    if AUTO_MERGE_LABEL not in pr.labels:
        return ActResult(
            move=move,
            status="skipped",
            detail=f"missing '{AUTO_MERGE_LABEL}' label",
        )
    r = client.put(
        f"{GITHUB_API}/repos/{move.repo}/pulls/{pr.number}/merge",
        headers=_gh_headers(),
        json={"merge_method": "squash"},
    )
    if r.status_code >= 300:
        return ActResult(move=move, status="error", detail=f"{r.status_code} {r.text}")
    return ActResult(move=move, status="executed", detail=f"merged #{pr.number}")


def _nudge_pipeline(client: httpx.Client, move: NextMove) -> ActResult:
    r = client.post(
        APPRAISAL_PIPELINE_TICK_URL,
        json={"reason": move.intent, "source": "motto-director"},
        timeout=30.0,
    )
    if r.status_code >= 300:
        return ActResult(move=move, status="error", detail=f"{r.status_code} {r.text}")
    return ActResult(move=move, status="executed", detail=str(r.status_code))


def act(
    moves: list[NextMove],
    snapshot: Snapshot,
    *,
    top_n: int = 5,
) -> list[ActResult]:
    """Execute the top-N moves. Honors DIRECTOR_DRY_RUN=1."""
    selected = moves[:top_n]
    if _dry_run():
        return [
            ActResult(move=m, status="dry_run", detail=f"would {m.kind}")
            for m in selected
        ]

    results: list[ActResult] = []
    with httpx.Client(timeout=30.0) as client:
        for move in selected:
            if move.kind == "noop":
                results.append(ActResult(move=move, status="skipped", detail="noop"))
                continue
            try:
                if move.kind == "file_issue":
                    results.append(_file_issue(client, move))
                elif move.kind == "spawn_session":
                    results.append(_spawn_session(client, move))
                elif move.kind == "merge_pr":
                    results.append(_merge_pr(client, move, snapshot))
                elif move.kind == "nudge_pipeline":
                    results.append(_nudge_pipeline(client, move))
            except httpx.HTTPError as exc:
                results.append(
                    ActResult(move=move, status="error", detail=str(exc))
                )
    return results
