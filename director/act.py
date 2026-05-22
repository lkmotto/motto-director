"""Act: execute top-N moves (file_issue, spawn_session, factory_droid,
merge_pr, nudge_pipeline, compound_pr)."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from director import factory_client, fleet, policy
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
from director.epics import Epic, EpicStep, insert_epics
from director.ideate import NextMove
from director.perceive import Issue, PullRequest, Snapshot, northflank_api_key

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


# main.py sets this before calling act(); act helpers read it to attach
# I/O capture (artifacts + decisions) to the right fleet run row. None
# means "no fleet run open" — capture functions then no-op.
fleet_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "fleet_run_id", default=None
)


def _fire_and_forget(coro) -> None:
    """Run an async fleet-capture coroutine from sync act() helpers.

    The sync `act()` is called from within `_run_async()` which is itself
    inside `asyncio.run`. We can't `await` here, so we schedule the coroutine
    on the running loop. If there is no loop (pure unit tests calling act()
    directly), we close the coroutine cleanly so we don't leak a 'coroutine
    was never awaited' warning. This is best-effort capture — the cycle
    must keep running even if the MCP server is down.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    loop.create_task(coro)


def _dry_run() -> bool:
    return os.environ.get("DIRECTOR_DRY_RUN", "0") == "1"


def _gh_headers() -> dict[str, str]:
    return {
        "Authorization": (
            f"Bearer {os.environ.get('GITHUB_TOKEN') or os.environ.get('GITHUB_PAT', '')}"
        ),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _claude_oauth_token() -> str:
    """Canonical secret is CLAUDE_CODE_OAUTH_TOKEN (Doppler motto-core/prd).

    Falls back to legacy CLAUDE_CODE_SESSION_TOKEN for backward compat
    until every deploy target has been repointed at motto-core/prd.
    """
    return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get(
        "CLAUDE_CODE_SESSION_TOKEN", ""
    )


def _claude_session_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_claude_oauth_token()}",
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


def _find_issue(snapshot: Snapshot, repo: str, title: str) -> Issue | None:
    for state in snapshot.repos:
        if state.repo != repo:
            continue
        for issue in state.open_issues:
            if issue.title == title or str(issue.number) in title:
                return issue
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


def _file_critique_issue(client: httpx.Client, move: NextMove) -> ActResult:
    """File a GitHub issue against the source repo of a flagged/blocked
    artifact. Distinguished from `_file_issue` by the explicit
    `output-critic` label so producing teams can filter and so the
    director can later report critic-driven activity separately.

    `move.rationale` is expected to contain the critic's structured
    findings (verdict, severity, issues list, suggested fix). The
    producing agent / artifact id are encoded in the title and intent.
    """
    body = (
        f"**Critic verdict:** {move.intent}\n\n"
        f"**Findings:**\n{move.rationale}\n\n"
        "_Filed by motto-director output_critic lens._"
    )
    r = client.post(
        f"{GITHUB_API}/repos/{move.repo}/issues",
        headers=_gh_headers(),
        json={
            "title": move.title,
            "body": body,
            "labels": ["output-critic"],
        },
    )
    if r.status_code >= 300:
        return ActResult(move=move, status="error", detail=f"{r.status_code} {r.text}")
    return ActResult(move=move, status="executed", detail=r.json().get("html_url", ""))


def _spawn_session(client: httpx.Client, move: NextMove, snapshot: Snapshot) -> ActResult:
    # Policy gate runs before we burn a session — this is the bandwidth saver.
    target = _find_issue(snapshot, move.repo, move.title) or _find_pr(
        snapshot, move.repo, move.title
    )
    if target is not None:
        eligible, reason = policy.is_eligible_for_spawn(target)
        if not eligible:
            return ActResult(move=move, status="skipped", detail=f"policy: {reason}")
    size = policy.estimate_session_diff_size(move.prompt_for_claude_code)
    if size > policy.MAX_FILES_PER_SESSION:
        return ActResult(
            move=move,
            status="skipped",
            detail=f"policy: prompt scope estimate {size} > {policy.MAX_FILES_PER_SESSION}",
        )
    if not _claude_oauth_token():
        _log("spawn_session.skipped", reason="no_oauth_token", repo=move.repo)
        return ActResult(
            move=move,
            status="skipped",
            detail="no CLAUDE_CODE_OAUTH_TOKEN; spawn_session is optional",
        )
    if not move.prompt_for_claude_code:
        return ActResult(move=move, status="skipped", detail="missing prompt_for_claude_code")

    fleet_run_id = fleet_run_id_var.get()
    pending_token = uuid.uuid4().hex[:12]

    # Capture the outbound prompt BEFORE the network call — we want a record
    # even if the spawn errors out.
    _fire_and_forget(
        fleet.record_artifact(
            run_id=fleet_run_id,
            kind="claude_session_prompt",
            ref=pending_token,
            meta={
                "prompt": move.prompt_for_claude_code,
                "intent": move.intent,
                "repo": move.repo,
            },
        )
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
    session_url = data.get("session_url") or data.get("id", "")

    response_summary = json.dumps(data, default=str)[:500]
    _fire_and_forget(
        fleet.record_artifact(
            run_id=fleet_run_id,
            kind="claude_session",
            ref=session_url,
            meta={
                "session_id": data.get("id"),
                "status_code": r.status_code,
                "response_summary": response_summary,
                "pending_token": pending_token,
            },
        )
    )
    _fire_and_forget(
        fleet.record_decision(
            run_id=fleet_run_id,
            choice="spawned_claude_session",
            rationale=move.intent,
            evidence={
                "repo": move.repo,
                "title": move.title,
                "session_url": session_url,
            },
        )
    )

    return ActResult(
        move=move,
        status="executed",
        detail=session_url,
    )


def _factory_api_key() -> str:
    return os.environ.get("FACTORY_API_KEY", "").strip()


def _resolve_droid_tag(move: NextMove) -> str:
    """Pick which custom droid to invoke based on the move's repo + intent.

    Maps to droid definitions under .factory/droids/*.md. Factory routes by
    droid tag on the session, not by model selection here, so we just need
    to attach the right tag string. Falls back to 'factory-orchestrator'
    (the meta-droid that decides and delegates).
    """
    repo = (move.repo or "").lower()
    intent = (move.intent or "").lower()
    title = (move.title or "").lower()
    haystack = f"{repo} {intent} {title}"
    if "doppler" in haystack or "secret" in haystack:
        return "doppler-sync"
    if "northflank" in haystack or "deploy" in haystack or "cron" in haystack:
        return "northflank-ops"
    if "fleet" in haystack or "report" in haystack or "audit" in haystack:
        return "ona-fleet-reporter"
    if (
        "mcp-server" in haystack
        or "motto-director" in haystack
        or "github" in haystack
        or "pr " in haystack
        or "merge" in haystack
    ):
        return "github-ops"
    return "factory-orchestrator"


async def _factory_spawn_async(prompt: str, tags: list[str]) -> dict[str, object]:
    """Async bridge: run the FactoryClient call from sync act() context."""
    client = factory_client.FactoryClient()
    session = await client.spawn_session(prompt=prompt, tags=tags)
    sid = client._extract_session_id(session)
    return {"session_id": sid, "raw": session}


def _spawn_factory_droid(move: NextMove, snapshot: Snapshot) -> ActResult:
    """Spawn a Factory droid session for this move.

    Parallel to _spawn_session (Claude Code) but routes to Factory's hosted
    droids using the .factory/droids/*.md role definitions. Per fleet doctrine
    we NEVER silently fall back to Claude — if FACTORY_API_KEY is missing we
    skip and let the director re-route on the next cycle. Honors the same
    policy gates as _spawn_session so a Factory droid can't run on a stale
    or oversized target either.
    """
    target = _find_issue(snapshot, move.repo, move.title) or _find_pr(
        snapshot, move.repo, move.title
    )
    if target is not None:
        eligible, reason = policy.is_eligible_for_spawn(target)
        if not eligible:
            return ActResult(move=move, status="skipped", detail=f"policy: {reason}")
    size = policy.estimate_session_diff_size(move.prompt_for_claude_code)
    if size > policy.MAX_FILES_PER_SESSION:
        return ActResult(
            move=move,
            status="skipped",
            detail=f"policy: prompt scope estimate {size} > {policy.MAX_FILES_PER_SESSION}",
        )
    if not _factory_api_key():
        _log("factory_droid.skipped", reason="no_api_key", repo=move.repo)
        return ActResult(
            move=move,
            status="skipped",
            detail=(
                "no FACTORY_API_KEY; factory_droid is optional. "
                "Doctrine: do not fall back to Claude Code."
            ),
        )
    if not move.prompt_for_claude_code:
        return ActResult(
            move=move,
            status="skipped",
            detail="missing prompt_for_claude_code (factory_droid reuses the prompt field)",
        )

    fleet_run_id = fleet_run_id_var.get()
    pending_token = uuid.uuid4().hex[:12]
    droid_tag = _resolve_droid_tag(move)
    tags = [f"droid:{droid_tag}", f"repo:{move.repo}", f"intent:{move.intent[:32]}"]

    _fire_and_forget(
        fleet.record_artifact(
            run_id=fleet_run_id,
            kind="factory_session_prompt",
            ref=pending_token,
            meta={
                "prompt": move.prompt_for_claude_code,
                "intent": move.intent,
                "repo": move.repo,
                "droid": droid_tag,
            },
        )
    )

    try:
        result = asyncio.run(_factory_spawn_async(prompt=move.prompt_for_claude_code, tags=tags))
    except RuntimeError:
        # We're already inside an event loop (act() is wrapped in _run_async).
        # Use a fresh loop in a worker thread so we don't collide.
        import threading

        result_holder: dict[str, object] = {}
        err_holder: dict[str, BaseException] = {}

        def _runner() -> None:
            try:
                result_holder["r"] = asyncio.run(
                    _factory_spawn_async(prompt=move.prompt_for_claude_code, tags=tags)
                )
            except BaseException as e:  # noqa: BLE001 — propagate any error type
                err_holder["e"] = e

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=60)
        if "e" in err_holder:
            return ActResult(
                move=move,
                status="error",
                detail=f"factory spawn failed: {err_holder['e']}",
            )
        if "r" not in result_holder:
            return ActResult(move=move, status="error", detail="factory spawn timed out (>60s)")
        result = result_holder["r"]  # type: ignore[assignment]
    except Exception as e:  # noqa: BLE001
        return ActResult(move=move, status="error", detail=f"factory spawn error: {e}")

    session_id = str(result.get("session_id", ""))
    session_url = f"https://factory.ai/sessions/{session_id}" if session_id else ""

    _fire_and_forget(
        fleet.record_artifact(
            run_id=fleet_run_id,
            kind="factory_session",
            ref=session_url,
            meta={
                "session_id": session_id,
                "droid": droid_tag,
                "pending_token": pending_token,
            },
        )
    )
    _fire_and_forget(
        fleet.record_decision(
            run_id=fleet_run_id,
            choice="spawned_factory_droid",
            rationale=move.intent,
            evidence={
                "repo": move.repo,
                "title": move.title,
                "droid": droid_tag,
                "session_url": session_url,
            },
        )
    )

    return ActResult(move=move, status="executed", detail=session_url)


def _verify_move(move: NextMove) -> ActResult:
    """Trigger an outcome verification on a previously-applied move.

    Director proposes this kind (manually approved like any other move)
    when it wants to confirm a prior move actually achieved its intent.
    The move's payload must contain `target_move_id` — the pending_moves
    row id whose outcome we want verified. The actual verifier dispatch
    lives in motto-mcp-server (verifiers/__init__.py); we just call the
    MCP tool here.

    Capability requests fired by the verifier flow back to the cockpit
    automatically; we record an inconclusive ActResult in that case so
    the move terminates and director can re-propose after grant.
    """
    target_id = move.target_move_id
    if target_id is None:
        return ActResult(
            move=move,
            status="error",
            detail="verify_move payload missing target_move_id",
        )
    try:
        target_id = int(target_id)
    except (ValueError, TypeError):
        return ActResult(
            move=move,
            status="error",
            detail=f"target_move_id not int: {target_id!r}",
        )

    if not (os.environ.get("MOTTO_MCP_URL") and os.environ.get("MOTTO_MCP_AUTH_TOKEN")):
        return ActResult(
            move=move,
            status="skipped",
            detail="verify_move: MOTTO_MCP_URL/TOKEN not configured",
        )

    async def _call() -> dict:
        from fastmcp import Client
        from fastmcp.client.auth import BearerAuth

        url = os.environ["MOTTO_MCP_URL"]
        token = os.environ["MOTTO_MCP_AUTH_TOKEN"]
        async with Client(url, auth=BearerAuth(token)) as c:
            resp = await c.call_tool(
                "verify_move",
                {
                    "move_id": target_id,
                    "requested_by": f"director:{fleet_run_id_var.get() or '?'}",
                },
            )
            data = getattr(resp, "data", None)
            if isinstance(data, dict):
                return data
            return {}

    try:
        result = asyncio.run(_call())
    except RuntimeError:
        # Already inside an event loop — act is normally sync, but the
        # cycle drain calls it from async context. Fall back to a fresh
        # loop in a thread to avoid "asyncio.run() cannot be called from
        # a running event loop".
        import threading

        box: dict = {}

        def _runner() -> None:
            box["r"] = asyncio.run(_call())

        t = threading.Thread(target=_runner)
        t.start()
        t.join(timeout=30)
        result = box.get("r", {})
    except Exception as exc:  # noqa: BLE001
        return ActResult(
            move=move,
            status="error",
            detail=f"verify_move call failed: {type(exc).__name__}: {exc}"[:200],
        )

    status = (result or {}).get("status") or "inconclusive"
    verifier = (result or {}).get("verifier") or "?"
    # Map verifier outcome to ActResult status. We treat verifier 'passed'
    # and 'failed' both as 'executed' (the verify ITSELF executed) — the
    # outcome lives in fleet.move_verifications and trust_scores.
    if status in ("passed", "failed"):
        return ActResult(
            move=move,
            status="executed",
            detail=f"verify[{verifier}]={status} target=#{target_id}",
        )
    if status == "inconclusive":
        return ActResult(
            move=move,
            status="executed",
            detail=f"verify[{verifier}]=inconclusive target=#{target_id}",
        )
    err = (result or {}).get("error") or "unknown"
    return ActResult(
        move=move,
        status="error",
        detail=f"verify[{verifier}]={status} err={err[:120]}",
    )


def _propose_epic(move: NextMove) -> ActResult:
    """Insert a self-heal Epic proposed by the output critic.

    The move carries the epic payload as a JSON blob in
    ``move.code_changes[0].content`` (path == ``"__epic__"``). We parse it,
    construct ``Epic``/``EpicStep`` objects, and insert at status='proposed'
    so it shows up in the cockpit approval queue. We deliberately do NOT
    auto-activate the epic — Luke approves Epics explicitly.

    Doctrine: Director nudges, never silently spawns work. propose_epic is
    a nudge in epic form — created by the critic when an artifact kind
    fails ``_SELF_HEAL_FAILURE_THRESHOLD`` times in one tick.
    """
    if not move.code_changes:
        return ActResult(
            move=move,
            status="error",
            detail="propose_epic: missing code_changes payload",
        )
    payload_change = move.code_changes[0]
    # code_changes items are dicts (see NextMove.code_changes typing in
    # ideate.py: list[dict[str, str]]). Earlier this read via getattr,
    # which silently returned "" for every dict and made propose_epic
    # a no-op in prod. Support both dict and object shapes defensively
    # so future refactors to a CodeChange dataclass don't re-break it.
    if isinstance(payload_change, dict):
        payload_raw = payload_change.get("content", "") or ""
    else:
        payload_raw = getattr(payload_change, "content", "") or ""
    if not payload_raw:
        return ActResult(
            move=move,
            status="error",
            detail="propose_epic: empty epic payload",
        )
    try:
        payload = json.loads(payload_raw)
    except (TypeError, ValueError) as exc:
        return ActResult(
            move=move,
            status="error",
            detail=f"propose_epic: bad JSON payload: {exc}"[:200],
        )
    if not isinstance(payload, dict):
        return ActResult(
            move=move,
            status="error",
            detail="propose_epic: payload not an object",
        )

    steps_raw = payload.get("steps") or []
    steps: list[EpicStep] = []
    for s in steps_raw:
        if not isinstance(s, dict):
            continue
        try:
            steps.append(
                EpicStep(
                    order=int(s.get("order", len(steps) + 1)),
                    title=str(s.get("title", ""))[:200],
                    kind=str(s.get("kind", "factory_droid"))[:64],
                    repo=str(s.get("repo", move.repo))[:120],
                    rationale=str(s.get("rationale", ""))[:1000],
                    depends_on=list(s.get("depends_on", []) or []),
                )
            )
        except (TypeError, ValueError):
            continue
    if not steps:
        return ActResult(
            move=move,
            status="error",
            detail="propose_epic: no valid steps in payload",
        )

    try:
        epic = Epic(
            title=str(payload.get("epic_title", move.title))[:200],
            kpi_ref=str(payload.get("kpi_ref", ""))[:200],
            rationale=str(payload.get("rationale", move.intent))[:1000],
            estimated_cycles=int(payload.get("estimated_cycles", 3)),
            success_criteria=str(payload.get("success_criteria", ""))[:1000],
            steps=steps,
        )
    except (TypeError, ValueError) as exc:
        return ActResult(
            move=move,
            status="error",
            detail=f"propose_epic: bad Epic fields: {exc}"[:200],
        )

    fleet_run_id = fleet_run_id_var.get() or "director-cycle"
    counts = insert_epics([epic], run_id=fleet_run_id)

    _fire_and_forget(
        fleet.record_decision(
            run_id=fleet_run_id,
            choice="proposed_self_heal_epic",
            rationale=move.intent,
            evidence={
                "epic_title": epic.title,
                "kpi_ref": epic.kpi_ref,
                "steps": len(epic.steps),
                "insert_counts": counts,
            },
        )
    )
    _fire_and_forget(
        fleet.record_artifact(
            run_id=fleet_run_id,
            kind="propose_epic_inserted",
            ref=epic.kpi_ref or epic.title[:64],
            meta={
                "epic_title": epic.title,
                "kpi_ref": epic.kpi_ref,
                "estimated_cycles": epic.estimated_cycles,
                "steps": [
                    {"order": s.order, "title": s.title, "kind": s.kind, "repo": s.repo}
                    for s in epic.steps
                ],
                "insert_counts": counts,
            },
        )
    )

    if counts.get("inserted", 0) >= 1:
        return ActResult(
            move=move,
            status="executed",
            detail=(
                f"epic proposed: '{epic.title[:80]}' "
                f"steps={len(epic.steps)} kpi={epic.kpi_ref[:60]}"
            ),
        )
    if counts.get("skipped", 0) >= 1:
        return ActResult(
            move=move,
            status="skipped",
            detail=f"epic already exists (unique violation): {epic.title[:120]}",
        )
    return ActResult(
        move=move,
        status="error",
        detail=f"epic insert failed: counts={counts}",
    )


def _merge_pr(client: httpx.Client, move: NextMove, snapshot: Snapshot) -> ActResult:
    pr = _find_pr(snapshot, move.repo, move.title)
    if pr is None:
        return ActResult(move=move, status="skipped", detail="PR not found in snapshot")
    eligible, reason = policy.is_eligible_for_auto_merge(pr, pr.ci_status)
    if not eligible:
        # Allow the legacy `auto-merge-ok` label as a temporary backstop
        # while we migrate review gates over to `director-ok`. Self-mod
        # and CI failures are NOT bypassable by either label.
        legacy_ok = (
            pr.ci_status == "success" and pr.approvals >= 1 and AUTO_MERGE_LABEL in pr.labels
        )
        if not legacy_ok:
            return ActResult(move=move, status="skipped", detail=f"policy: {reason}")
    r = client.put(
        f"{GITHUB_API}/repos/{move.repo}/pulls/{pr.number}/merge",
        headers=_gh_headers(),
        json={"merge_method": "squash"},
    )
    if r.status_code >= 300:
        return ActResult(move=move, status="error", detail=f"{r.status_code} {r.text}")

    _fire_and_forget(
        fleet.record_decision(
            run_id=fleet_run_id_var.get(),
            choice="merged_pr",
            rationale=move.intent,
            evidence={
                "repo": move.repo,
                "pr_number": pr.number,
                "url": pr.url,
                "ci_status": pr.ci_status,
                "approvals": pr.approvals,
            },
        )
    )

    return ActResult(move=move, status="executed", detail=f"merged #{pr.number}")


def _log(event: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


def _compound_pr(client: httpx.Client, move: NextMove, *, run_id: str) -> ActResult:
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
    _fire_and_forget(
        fleet.record_decision(
            run_id=fleet_run_id_var.get(),
            choice="compound_pr_appended",
            rationale=move.rationale,
            evidence={
                "repo": move.repo,
                "pr_number": pr_number,
                "branch": branch,
                "commit_sha": commit_sha,
                "moves_in_pr": len(entries),
            },
        )
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
        return [ActResult(move=m, status="dry_run", detail=f"would {m.kind}") for m in selected]

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
                elif move.kind == "file_critique_issue":
                    results.append(_file_critique_issue(client, move))
                elif move.kind == "spawn_session":
                    results.append(_spawn_session(client, move, snapshot))
                elif move.kind == "factory_droid":
                    results.append(_spawn_factory_droid(move, snapshot))
                elif move.kind == "merge_pr":
                    results.append(_merge_pr(client, move, snapshot))
                elif move.kind == "nudge_pipeline":
                    results.append(_nudge_pipeline(client, move))
                elif move.kind == "compound_pr":
                    results.append(_compound_pr(client, move, run_id=run_id))
                elif move.kind == "verify_move":
                    results.append(_verify_move(move))
                elif move.kind == "propose_epic":
                    results.append(_propose_epic(move))
            except httpx.HTTPError as exc:
                results.append(ActResult(move=move, status="error", detail=str(exc)))
    return results
