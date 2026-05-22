"""Output critic — director sees + critiques actual fleet outputs.

Per Luke (May 2026): "I want the director to have a lot of visibility not
just to via otel, shared state DB and langfuse, I want it to see and
critique outputs."

Fleet agents store their real outputs (PR diffs, draft emails, scripts,
comp narratives, AMC reply drafts) inline in `fleet.artifacts.content`
JSONB via the MCP tool `record_artifact_content`. Each artifact starts
with `review_status='pending'`.

Each director tick, this module:
  1. Calls MCP `artifacts_pending_review` to fetch unreviewed artifacts.
  2. Fans out parallel DeepSeek critic calls (capped, default 5 — same
     ceiling as the rest of the orchestrator) to judge each artifact's
     body against its declared intent.
  3. Calls MCP `mark_artifact_reviewed` to set the verdict
     ('passed' | 'flagged' | 'blocked') and record the critique payload.
  4. For 'flagged' or 'blocked' artifacts, emits a `file_critique_issue`
     NextMove targeted at the source repo. The move is queued through
     the normal pending_moves approval flow — the director never
     auto-files critic issues without human approval.

Doctrine constraints (preserve verbatim from session):
  - DeepSeek only. No Claude Code anywhere in this workflow.
  - If a capability is missing (e.g. vision for image artifacts),
    REPORT the gap by emitting a flagged verdict with critique noting
    'capability_gap: vision_required'. Do NOT silently fall back to
    another model.
  - send_blocking artifacts (cold emails, AMC replies) MUST be reviewed
    before downstream code dispatches them. The downstream agent reads
    review_status; this module only writes it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from director.ideate import NextMove, _coerce_move

DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_TIMEOUT_S = float(os.environ.get("DEEPSEEK_TIMEOUT_S", "120"))

# Body bytes shown to the critic. Larger-than-this is summarized in the
# truncated flag the MCP server already set when storing.
CRITIC_BODY_CHAR_CAP = 24_000

# Verdicts the critic is allowed to return.
_VALID_VERDICTS: frozenset[str] = frozenset({"pass", "flag", "block"})

# Artifact kinds that are typically image-bearing — the critic flags
# these as capability_gap until vision support is added.
_VISION_REQUIRED_KINDS: frozenset[str] = frozenset(
    {
        "thumbnail",
        "image",
        "screenshot",
        "video_frame",
    }
)


def _log(event: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


CRITIC_SYSTEM_PROMPT = """You are the output critic for the motto stack.

Your job: read ONE artifact produced by a fleet agent and judge whether
it is fit for purpose. The agent has declared an `intent` — what it was
trying to accomplish. Judge the body against that intent.

Verdicts (pick exactly one):
  - "pass":  body adequately fulfills intent. No issues that would
             block sending / merging / shipping.
  - "flag":  body has problems but is salvageable. Issues are concrete
             but non-critical. Note: even high-severity problems should
             still be 'flag' if a human review can fix them — reserve
             'block' for truly send-unsafe content.
  - "block": body is send-unsafe. Examples: includes private secrets,
             contains hallucinated facts in an outbound message,
             violates AppraisalOS doctrine (auto-sending to AMCs),
             violates TOTAL doctrine (clicking Sign/Deliver/Share),
             or impersonates a person without consent.

Severity (only when verdict is flag/block): one of "low" | "medium" | "high".

Issues: an array of short strings, each describing ONE concrete problem
with a quote or pointer into the body. Keep each issue under 200 chars.

suggested_fix: ONE concrete action the producing agent should take.
1-2 sentences. Concrete. Actionable. No vague advice.

Output STRICT JSON, no prose, no fences:
{
  "verdict": "pass" | "flag" | "block",
  "severity": "low" | "medium" | "high" | null,
  "issues": [string, ...],
  "suggested_fix": string
}

If the artifact is image-bearing (e.g. screenshot, thumbnail) and the
body field doesn't actually contain the image, return verdict "flag",
severity "low", issues ["capability_gap: vision_required"], and
suggested_fix "Wire vision model into critic before judging
image-bearing artifacts."

Do NOT propose moves. The orchestrator handles that.
"""


@dataclass
class CritiqueResult:
    artifact_id: int
    agent_name: str
    kind: str
    verdict: str  # pass | flag | block
    severity: str | None
    issues: list[str]
    suggested_fix: str
    repo: str
    intent: str
    send_blocking: bool
    tokens_in: int
    tokens_out: int
    latency_ms: int
    error: str | None = None


def _coerce_verdict(raw: dict) -> tuple[str, str | None, list[str], str]:
    """Pull verdict / severity / issues / suggested_fix out of LLM JSON.

    Returns ('flag', 'low', [...], '...') as a defensive fallback when
    the LLM returns shape we don't recognize — better to surface a
    flag than to drop the artifact silently.
    """
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in _VALID_VERDICTS:
        verdict = "flag"
    severity_raw = raw.get("severity")
    severity: str | None
    if isinstance(severity_raw, str) and severity_raw.lower() in ("low", "medium", "high"):
        severity = severity_raw.lower()
    else:
        severity = None if verdict == "pass" else "low"
    issues_raw = raw.get("issues") or []
    issues: list[str] = []
    if isinstance(issues_raw, list):
        for item in issues_raw[:20]:
            if not isinstance(item, str):
                item = str(item)
            issues.append(item[:200])
    suggested_fix = str(raw.get("suggested_fix") or "").strip()[:500]
    return verdict, severity, issues, suggested_fix


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        parts = text.split("```", 2)
        text = parts[1] if len(parts) >= 2 else ""
    text = text.strip()
    if text.startswith("json"):
        text = text[4:].strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return {}


def _build_user_message(artifact: dict[str, Any]) -> str:
    content = artifact.get("content") or {}
    body = str(content.get("body") or "")
    if len(body) > CRITIC_BODY_CHAR_CAP:
        body = body[:CRITIC_BODY_CHAR_CAP] + "\n\n[...truncated for critic prompt]"
    intent = str(content.get("intent") or "(no intent declared)")
    repo = str(content.get("repo") or "(unknown)")
    truncated_at_storage = bool(content.get("truncated"))
    send_blocking = bool(content.get("send_blocking"))
    return (
        f"# Artifact #{artifact.get('id')}\n"
        f"agent: {artifact.get('agent_name')}\n"
        f"kind: {artifact.get('kind')}\n"
        f"name: {artifact.get('name') or '(none)'}\n"
        f"source repo: {repo}\n"
        f"send_blocking: {send_blocking}\n"
        f"truncated_at_storage: {truncated_at_storage}\n\n"
        f"## Declared intent\n{intent}\n\n"
        f"## Body\n{body}\n\n"
        "Judge this artifact and return STRICT JSON per the schema."
    )


async def _critique_one(
    client: httpx.AsyncClient,
    *,
    artifact: dict[str, Any],
    api_key: str,
    model: str,
) -> CritiqueResult:
    artifact_id = int(artifact.get("id", 0))
    agent_name = str(artifact.get("agent_name") or "")
    kind = str(artifact.get("kind") or "")
    content = artifact.get("content") or {}
    repo = str(content.get("repo") or "")
    intent = str(content.get("intent") or "")
    send_blocking = bool(content.get("send_blocking"))

    # Capability gap: skip the LLM entirely for image-bearing kinds.
    # Doctrine: report the gap, don't silently fall back to a different
    # model (per Luke's "if you are needing additional resources report
    # if you need vision").
    if kind in _VISION_REQUIRED_KINDS:
        _log("critic.capability_gap", artifact_id=artifact_id, kind=kind)
        return CritiqueResult(
            artifact_id=artifact_id,
            agent_name=agent_name,
            kind=kind,
            verdict="flag",
            severity="low",
            issues=["capability_gap: vision_required"],
            suggested_fix=(
                "Wire vision model into output critic before judging "
                f"{kind} artifacts. DeepSeek text-only cannot evaluate image bodies."
            ),
            repo=repo,
            intent=intent,
            send_blocking=send_blocking,
            tokens_in=0,
            tokens_out=0,
            latency_ms=0,
        )

    user_msg = _build_user_message(artifact)
    started = datetime.now(UTC)
    try:
        resp = await client.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "messages": [
                    {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            },
            timeout=DEEPSEEK_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
        _log(
            "critic.error",
            artifact_id=artifact_id,
            error=str(exc)[:200],
            latency_ms=latency,
        )
        return CritiqueResult(
            artifact_id=artifact_id,
            agent_name=agent_name,
            kind=kind,
            verdict="flag",
            severity="low",
            issues=[f"critic_call_failed: {str(exc)[:120]}"],
            suggested_fix="Retry critique on next director tick.",
            repo=repo,
            intent=intent,
            send_blocking=send_blocking,
            tokens_in=0,
            tokens_out=0,
            latency_ms=latency,
            error=str(exc)[:200],
        )

    latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    usage = data.get("usage", {}) or {}
    parsed = _extract_json(text)
    if not isinstance(parsed, dict):
        parsed = {}
    verdict, severity, issues, suggested_fix = _coerce_verdict(parsed)

    _log(
        "critic.done",
        artifact_id=artifact_id,
        agent=agent_name,
        kind=kind,
        verdict=verdict,
        severity=severity,
        issue_count=len(issues),
        latency_ms=latency,
    )

    return CritiqueResult(
        artifact_id=artifact_id,
        agent_name=agent_name,
        kind=kind,
        verdict=verdict,
        severity=severity,
        issues=issues,
        suggested_fix=suggested_fix,
        repo=repo,
        intent=intent,
        send_blocking=send_blocking,
        tokens_in=int(usage.get("prompt_tokens", 0) or 0),
        tokens_out=int(usage.get("completion_tokens", 0) or 0),
        latency_ms=latency,
    )


def _result_to_move(r: CritiqueResult) -> NextMove | None:
    """Convert a non-pass critique into a `file_critique_issue` move.

    Returns None for verdict='pass' (no action needed) and for results
    with no source repo (can't file an issue without a target).
    """
    if r.verdict == "pass":
        return None
    if not r.repo:
        # Defensive — without a repo we have nowhere to file. Log and
        # let the next tick re-pick this up if the producer fixes meta.
        _log(
            "critic.move_dropped",
            artifact_id=r.artifact_id,
            reason="no_repo_in_artifact_meta",
        )
        return None
    # Priority: block > flag-high > flag-medium > flag-low.
    severity_priority = {"high": 1, "medium": 2, "low": 3}
    if r.verdict == "block":
        priority = 1
    else:
        priority = severity_priority.get(r.severity or "low", 3) + 1
    priority = max(1, min(5, priority))

    issues_block = "\n".join(f"- {i}" for i in r.issues) or "- (no issues listed)"
    rationale = (
        f"Verdict: **{r.verdict}** "
        f"(severity: {r.severity or 'n/a'})\n\n"
        f"Producing agent: `{r.agent_name}`\n"
        f"Artifact id: `{r.artifact_id}`\n"
        f"Artifact kind: `{r.kind}`\n"
        f"Send-blocking: {r.send_blocking}\n\n"
        f"### Issues\n{issues_block}\n\n"
        f"### Suggested fix\n{r.suggested_fix or '(none)'}"
    )
    title = (f"[output-critic] {r.verdict.upper()}: {r.agent_name} {r.kind} #{r.artifact_id}")[:240]
    intent = (
        f"output_critic flagged artifact #{r.artifact_id} "
        f"({r.kind}) from {r.agent_name}; "
        f"verdict={r.verdict} severity={r.severity or 'n/a'}; "
        f"send_blocking={r.send_blocking}."
    )[:500]
    return _coerce_move(
        {
            "repo": r.repo,
            "kind": "file_critique_issue",
            "title": title,
            "rationale": rationale[:1000],
            "prompt_for_claude_code": "",
            "priority": priority,
            "intent": intent,
        }
    )


async def _fetch_pending(
    *,
    since_hours: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Pull pending-review artifacts via MCP. Empty list on any failure
    (this lens then no-ops for the tick — same shape as parallel_ideate).
    """
    if not (os.environ.get("MOTTO_MCP_URL") and os.environ.get("MOTTO_MCP_AUTH_TOKEN")):
        _log("critic.skipped", reason="mcp_not_configured")
        return []
    try:
        # Local import: keep fastmcp out of module-load path so unit
        # tests can monkeypatch _fetch_pending without touching network.
        from fastmcp import Client
        from fastmcp.client.auth import BearerAuth

        url = os.environ["MOTTO_MCP_URL"]
        token = os.environ["MOTTO_MCP_AUTH_TOKEN"]
        async with Client(url, auth=BearerAuth(token)) as c:
            resp = await c.call_tool(
                "artifacts_pending_review",
                {"since_hours": int(since_hours), "limit": int(limit)},
            )
            data = getattr(resp, "data", None)
            if isinstance(data, list):
                return data
            return []
    except Exception as exc:  # noqa: BLE001
        _log("critic.fetch_failed", error=str(exc)[:200])
        return []


async def _mark_reviewed(
    *,
    artifact_id: int,
    review_status: str,
    critique: dict[str, Any],
) -> bool:
    """Write the verdict back via MCP. Best-effort — failures don't block
    the move from being queued (the move itself carries the critique).
    """
    if not (os.environ.get("MOTTO_MCP_URL") and os.environ.get("MOTTO_MCP_AUTH_TOKEN")):
        return False
    try:
        from fastmcp import Client
        from fastmcp.client.auth import BearerAuth

        url = os.environ["MOTTO_MCP_URL"]
        token = os.environ["MOTTO_MCP_AUTH_TOKEN"]
        async with Client(url, auth=BearerAuth(token)) as c:
            resp = await c.call_tool(
                "mark_artifact_reviewed",
                {
                    "artifact_id": int(artifact_id),
                    "review_status": review_status,
                    "critique": critique,
                },
            )
            data = getattr(resp, "data", None)
            return bool(isinstance(data, dict) and data.get("ok"))
    except Exception as exc:  # noqa: BLE001
        _log(
            "critic.mark_failed",
            artifact_id=artifact_id,
            error=str(exc)[:200],
        )
        return False


def _verdict_to_status(verdict: str) -> str:
    return {
        "pass": "passed",
        "flag": "flagged",
        "block": "blocked",
    }.get(verdict, "flagged")


async def critique_artifacts(
    *,
    since_hours: int = 24,
    max_per_tick: int = 25,
    max_concurrency: int = 5,
) -> list[NextMove]:
    """Director's `output_critic` lens.

    Mirrors the shape of `orchestrator.parallel_ideate`: returns a list
    of NextMove the orchestrator can merge with other lenses' output.

    The function:
      1. Fetches pending-review artifacts from MCP.
      2. Fans out parallel DeepSeek critiques (capped concurrency).
      3. Writes each verdict back via mark_artifact_reviewed.
      4. Returns file_critique_issue moves for non-pass verdicts.

    Returns an empty list on any configuration error (no DeepSeek key,
    no MCP, no pending artifacts) — caller treats this lens as
    optional, identical to the rest of the orchestrator.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        _log("critic.skipped", reason="DEEPSEEK_API_KEY missing")
        return []

    pending = await _fetch_pending(since_hours=since_hours, limit=max_per_tick)
    if not pending:
        _log("critic.no_pending")
        return []

    model = DEEPSEEK_DEFAULT_MODEL
    sem = asyncio.Semaphore(max_concurrency)

    async with httpx.AsyncClient() as client:

        async def _run(art: dict[str, Any]) -> CritiqueResult:
            async with sem:
                return await _critique_one(
                    client,
                    artifact=art,
                    api_key=api_key,
                    model=model,
                )

        results = await asyncio.gather(*(_run(a) for a in pending))

    # Write verdicts back (concurrent, best-effort).
    async def _write_back(r: CritiqueResult) -> None:
        critique_payload = {
            "verdict": r.verdict,
            "severity": r.severity,
            "issues": r.issues,
            "suggested_fix": r.suggested_fix,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "latency_ms": r.latency_ms,
            "error": r.error,
        }
        await _mark_reviewed(
            artifact_id=r.artifact_id,
            review_status=_verdict_to_status(r.verdict),
            critique=critique_payload,
        )

    await asyncio.gather(*(_write_back(r) for r in results))

    moves: list[NextMove] = []
    for r in results:
        m = _result_to_move(r)
        if m is not None:
            moves.append(m)

    pass_count = sum(1 for r in results if r.verdict == "pass")
    flag_count = sum(1 for r in results if r.verdict == "flag")
    block_count = sum(1 for r in results if r.verdict == "block")
    err_count = sum(1 for r in results if r.error)
    _log(
        "critic.tick.done",
        reviewed=len(results),
        passed=pass_count,
        flagged=flag_count,
        blocked=block_count,
        errored=err_count,
        moves_emitted=len(moves),
    )
    return moves


def is_enabled() -> bool:
    """True when DIRECTOR_OUTPUT_CRITIC env is set to a truthy value.

    Default OFF until rolled out, mirroring DIRECTOR_PARALLEL_SUBAGENTS.
    """
    return os.environ.get("DIRECTOR_OUTPUT_CRITIC", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
