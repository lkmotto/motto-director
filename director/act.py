"""Act: execute top-N moves (file_issue, spawn_session, merge_pr,
nudge_pipeline, compound_pr)."""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from director.compound import (
    CodeChange,
    CompoundEntry,
    allow_self_mod,
    append_commit,
    auto_merge_enabled_env,
    compound_branch_name,
    compound_max_moves,
    enable_auto_merge,
    ensure_branch,
    ensure_pr,
    is_self_mod,
    parse_pr_entries,
    update_pr_body,
)
from director.ideate import NextMove
from director.perceive import PullRequest, Snapshot, northflank_api_key

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
    if not os.environ.get("CLAUDE_CODE_SESSION_TOKEN"):
        _log("spawn_session.skipped", reason="no_session_token", repo=move.repo)
        return ActResult(
            move=move,
            status="skipped",
            detail="no CLAUDE_CODE_SESSION_TOKEN; spawn_session is optional",
        )
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


def _log(event: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


def _compound_pr(
    client: httpx.Client, move: NextMove, *, run_id: str
) -> ActResult:
    if not move.code_changes:
        return ActResult(move=move, status="skipped", detail="no code_changes")

    paths = [c["path"] for c in move.code_changes]
    if is_self_mod(move.repo, paths) and not allow_self_mod():
        return ActResult(
            move=move,
            status="skipped",
            detail="self-modifying path; set DIRECTOR_ALLOW_SELF_MOD=true to enable",
        )

    branch = compound_branch_name()
    ensure_branch(client, move.repo, branch)
    pr = ensure_pr(client, move.repo, branch)
    pr_number = pr["number"]
    pr_node_id = pr["node_id"]

    entries = parse_pr_entries(pr.get("body"))
    commit_message = json.dumps(
        {
            "director_run_id": run_id,
            "move_kind": move.kind,
            "rationale": move.rationale,
        }
    )
    changes = [CodeChange(path=c["path"], content=c["content"]) for c in move.code_changes]
    commit_sha = append_commit(client, move.repo, branch, changes, commit_message)

    entries.append(
        CompoundEntry(
            ts=datetime.now(UTC).isoformat(),
            move_kind=move.kind,
            title=move.title,
            rationale=move.rationale,
            commit_sha=commit_sha,
        )
    )
    update_pr_body(client, move.repo, pr_number, entries)
    _log(
        "director.compound_appended",
        repo=move.repo,
        pr_number=pr_number,
        moves_in_pr=len(entries),
    )

    max_moves = compound_max_moves()
    flush_reason: str | None = None
    if len(entries) >= max_moves:
        flush_reason = f"max_moves_reached({max_moves})"
    elif auto_merge_enabled_env():
        flush_reason = "auto_merge_env"

    if flush_reason is not None:
        try:
            enable_auto_merge(client, pr_node_id)
            _log(
                "director.auto_merge_enabled",
                repo=move.repo,
                pr_number=pr_number,
            )
            _log(
                "director.compound_flushed",
                repo=move.repo,
                pr_number=pr_number,
                reason=flush_reason,
            )
        except httpx.HTTPError as exc:
            return ActResult(
                move=move,
                status="executed",
                detail=f"appended #{pr_number}; auto-merge failed: {exc}",
            )

    return ActResult(
        move=move,
        status="executed",
        detail=f"appended to #{pr_number} ({len(entries)} moves)",
    )


def _nudge_pipeline(client: httpx.Client, move: NextMove) -> ActResult:
    headers: dict[str, str] = {}
    nf_key = northflank_api_key()
    if nf_key:
        headers["Authorization"] = f"Bearer {nf_key}"
    r = client.post(
        APPRAISAL_PIPELINE_TICK_URL,
        json={"reason": move.intent, "source": "motto-director"},
        headers=headers,
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
    run_id: str | None = None,
) -> list[ActResult]:
    """Execute the top-N moves. Honors DIRECTOR_DRY_RUN=1."""
    selected = moves[:top_n]
    if _dry_run():
        return [
            ActResult(move=m, status="dry_run", detail=f"would {m.kind}")
            for m in selected
        ]

    run_id = run_id or uuid.uuid4().hex[:12]
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
                elif move.kind == "compound_pr":
                    results.append(_compound_pr(client, move, run_id=run_id))
            except httpx.HTTPError as exc:
                results.append(
                    ActResult(move=move, status="error", detail=str(exc))
                )
    return results
