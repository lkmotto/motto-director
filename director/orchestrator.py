"""Parallel subagent orchestrator.

The default `ideate()` calls one DeepSeek with a single generalist system
prompt. That works, but it bottlenecks on a single LLM's attention across
five very different concerns (CI, PR aging, issue triage, cross-repo
coupling, cost). It also can't fan out to look at problems in parallel —
which is the whole point the user asked for.

This module adds `parallel_ideate(snapshot)`: it runs N specialized
DeepSeek subagents concurrently via `asyncio.gather`, each with a
focused lens, then merges + dedupes their proposals into a single
ranked list of NextMoves.

Each subagent gets the same Snapshot but a different system prompt that
tells it which lens to apply, what to look for, and what to ignore.

Default lenses (8):
  Maintenance lenses (small, narrow):
  - ci_doctor:        red CI, flaky tests, broken builds
  - stale_pr_closer:  PRs idle >24h with green CI; propose merge or rebase
  - issue_triager:    cluster open issues; merge dupes / close stale / promote
  - cross_repo:       one repo's change implies follow-up in another
  - cost_watchdog:    Langfuse + NF metrics regressions

  Strategic lenses (big, ambitious):
  - architect:        propose structural upgrades (new agents, new lenses,
                      consolidations, simplifications) — uses STRATEGIC_INTENT
                      to know what we're trying to *build*
  - opportunity_scout: looks at what's *missing* (capability gaps, unwritten
                      tests, manual workflows that should be automated)
  - epic_bundler:     after the other lenses run, take their proposals and
                      bundle related ones into single compound sessions

Activation: set DIRECTOR_PARALLEL_SUBAGENTS=1 on the NF job env. When
unset (or 0), main.py uses the legacy `ideate()` path.

Cost: ~5x a single ideate call (~$0.005-0.01 per tick at V4-Flash).
Latency: ~max single-call latency (~3-8s) since calls are concurrent.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import httpx

from director import epic_executor as epic_executor_mod
from director.epics import Epic, EpicStep, count_open_kpis, insert_epics
from director.ideate import NextMove, _coerce_move
from director.kpis import format_for_prompt as format_kpis
from director.kpis import load_kpis
from director.perceive import Snapshot
from director.strategy import format_for_prompt, load_strategic_intent

DEEPSEEK_BASE_URL = os.environ.get(
    "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
)
DEEPSEEK_DEFAULT_MODEL = os.environ.get(
    "DEEPSEEK_MODEL", "deepseek-v4-flash"
)
DEEPSEEK_TIMEOUT_S = float(os.environ.get("DEEPSEEK_TIMEOUT_S", "120"))


@dataclass
class SubagentResult:
    lens: str
    moves: list[NextMove]
    tokens_in: int
    tokens_out: int
    latency_ms: int
    error: str | None = None


def _log(event: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


# ---------------------------------------------------------------------------
# Lens definitions
# ---------------------------------------------------------------------------

_BASE_RULES = """Output STRICT JSON: {"moves": [NextMove, ...]} with no prose.

Each NextMove has:
  - repo: "lkmotto/<repo>"
  - kind: one of spawn_session | file_issue | merge_pr | nudge_pipeline |
          compound_pr | noop
  - title: short imperative
  - rationale: 1-3 sentences referencing concrete signals
  - prompt_for_claude_code: required for spawn_session, self-contained brief
  - priority: integer 1 (highest) to 5 (lowest)
  - intent: 1-2 sentences explaining WHY NOW citing PR numbers/ages/statuses
  - code_changes: list of {path, content} for compound_pr only

Hard rules:
1. Every move MUST include `intent` referencing concrete signals.
2. Stay within YOUR lens. Do not propose moves outside your scope.
3. Output empty moves list if your lens has nothing to do this tick.
4. Keep proposals small and high-signal. Quality > quantity. Max 5 moves.
"""

LENS_PROMPTS: dict[str, str] = {
    "ci_doctor": """You are the CI doctor for the motto stack.

Lens: failed CI runs, flaky tests, broken builds, deploy failures.

Look for:
  - Open PRs whose checks include FAILURE / FAILED status
  - Recent CI runs with non-success conclusion
  - Repeating test failures across multiple PRs (probable flake)
  - Deploy/build job statuses that are FAILED or stuck

Propose:
  - spawn_session moves with a Claude Code prompt that fetches the failure
    log and proposes a fix
  - file_issue moves for flakes that need triage but not immediate fix
  - merge_pr moves for PRs where the CI failure is a known transient

Ignore: feature work, doc PRs, issue triage. That's other lenses.

""" + _BASE_RULES,

    "stale_pr_closer": """You are the stale-PR closer for the motto stack.

Lens: open PRs that have been sitting too long.

Look for:
  - Open PRs older than 24 hours with green CI (candidates to merge)
  - Open PRs older than 48 hours regardless of CI (candidates to rebase or close)
  - Approved PRs not yet merged

Propose:
  - merge_pr moves for green + approved + idle >24h
  - spawn_session moves for stale PRs needing rebase (Claude Code prompt to
    rebase onto main and push)
  - file_issue moves to escalate PRs that need human decision

Ignore: brand-new PRs (<24h), CI failures (CI doctor lens), feature ideation.

""" + _BASE_RULES,

    "issue_triager": """You are the issue triager for the motto stack.

Lens: open GitHub issues.

Look for:
  - Duplicate or near-duplicate issue titles
  - Stale issues (>14 days, no activity, no recent comments)
  - High-signal issues that should be promoted to PRs (clear scope, single repo)
  - Issues that are actually questions or discussions in disguise

Propose:
  - file_issue moves with body "DUPE OF #N — closing" for clear duplicates
  - spawn_session moves to promote a well-scoped issue to a PR (Claude Code
    prompt should reference the issue number and the desired outcome)
  - file_issue moves to nudge stale issues with a check-in question

Ignore: PR queue, CI failures, code review.

""" + _BASE_RULES,

    "cross_repo": """You are the cross-repo coupling watcher for the motto stack.

Lens: changes in one repo that imply required follow-up in another.

Look for:
  - Recent merges in motto-mcp-server that changed an HTTP endpoint or
    schema → consumers (motto-director, motto-appraisal-pipeline) may need
    to update
  - Schema migrations in any repo that imply a corresponding code change
  - Renames or deprecations in shared types
  - New ENV vars introduced that other repos should set

Propose:
  - file_issue moves on the downstream repo describing the upstream change
  - spawn_session moves with a Claude Code prompt to make the follow-up edit

Ignore: single-repo work, CI, issue triage, cost.

""" + _BASE_RULES,

    "cost_watchdog": """You are the cost / performance watchdog for the motto stack.

Lens: anomalies in cost or latency.

Look for:
  - Langfuse traces with unusually high token counts (>3x median)
  - Northflank job runs that doubled their typical duration
  - Repeated retries indicating wasted spend
  - DeepSeek calls that should be using V4-Flash but landed on V4-Pro

Propose:
  - file_issue moves describing the regression with concrete numbers
  - spawn_session moves to investigate the root cause
  - noop moves are fine if the lens has no signal this tick

Ignore: PR queue, CI, issue triage, cross-repo.

""" + _BASE_RULES,
}

LENS_PROMPTS["architect"] = """You are the fleet architect for the motto stack.

Lens: structural upgrades — the kind of moves that make the system *better*,
not just unbroken. Read the STRATEGIC INTENT block carefully; every move you
propose should advance one of the listed priorities or remove an item from
the "Things to avoid" list.

Look for:
  - Capability gaps in the fleet (an agent that should exist but doesn't)
  - Lens gaps in motto-director itself (your own blind spots)
  - Consolidation opportunities (two repos that should be one, two
    Doppler projects that should be one)
  - Patterns of repeated manual work that should be automated
  - Pieces of the 90-day priorities that have stalled
  - Self-improvement: motto-director should propose PRs against itself
    when DIRECTOR_ALLOW_SELF_MOD is enabled

Propose:
  - compound_pr moves with concrete code_changes for small, surgical
    self-modifications (new lens, prompt tweak, new policy gate)
  - spawn_session moves for larger refactors. Be ambitious with scope:
    a single session that touches 2-3 related files and rebases a stalled
    PR is BETTER than 3 separate sessions.
  - file_issue moves to capture an architectural decision Luke needs to
    make ("should we promote X to its own repo?")

Ignore: day-to-day CI failures, individual stale PRs, duplicate issues.
Other lenses cover those.

Bias: prefer one big high-leverage move over five small ones. Empty list
is better than a small move padding the count.

""" + _BASE_RULES


LENS_PROMPTS["opportunity_scout"] = """You are the opportunity scout for the motto stack.

Lens: things that are *missing*, not things that are broken. The director's
other lenses are janitorial — you find net-new value.

Look for:
  - Capabilities the strategic intent calls for but no agent owns yet
  - Tests that don't exist for code paths that already shipped
  - Observability gaps (an agent with no Langfuse traces, a service with
    no /health endpoint, a job with no run-status query)
  - Knowledge artifacts not yet captured (a runbook that should exist,
    a CLAUDE.md missing for a repo, a postmortem not written)
  - External integrations Luke uses manually but no agent does
    (Apollo enrichment, Lavender scoring, Plaid sync, etc.)
  - Wins blocked by a single small missing piece

Propose:
  - file_issue moves naming the gap explicitly with rationale citing why
    it matters now
  - spawn_session moves to fill the gap (Claude Code prompt that creates
    the missing file/test/runbook)
  - compound_pr moves with concrete content for small additive files

Ignore: existing issues being worked on, PRs in flight, CI noise.

Bias: name what's missing in plain language. Don't propose if you can't
cite a concrete reason it matters this cycle.

""" + _BASE_RULES


LENS_PROMPTS["planner"] = """You are the multi-cycle planner for the motto stack.

This lens runs ONLY at deep_read=heavy. Your job is to look at the KPI
gaps in the user message under `=== NORTH-STAR KPIs ===` and propose 1-3
epics — multi-step projects that will move a specific KPI over multiple
cycles. You are the antidote to single-redundancy janitorial work; think
in 3-8 step plans, not one-off moves.

Look for:
  - KPIs whose current value is far from target (the bigger the gap, the
    higher priority)
  - Strategic priorities from STRATEGIC INTENT that no current epic owns
  - Patterns of repeated manual work (gap that warrants a multi-step
    automation effort, not just one issue)
  - Capability gaps that block multiple downstream wins

DO NOT propose:
  - An epic for a KPI already in the `=== OPEN KPI EPICS ===` list — the
    planner only proposes one epic per KPI at a time.
  - Single-step "epics" — those are normal moves; let other lenses handle.
  - Plans with more than 8 steps; if scope is bigger, split into a phase 1.

Output STRICT JSON shape (NOT a NextMove list — DIFFERENT schema):
  {
    "epics": [
      {
        "title": "short imperative title",
        "kpi_ref": "exact KPI name from the KPIs block (case-sensitive)",
        "rationale": "1-3 sentences citing the KPI gap + strategic intent",
        "estimated_cycles": 5,
        "success_criteria": "observable outcome that closes this epic",
        "steps": [
          {
            "order": 1,
            "title": "short imperative",
            "kind": "spawn_session" | "file_issue" | "compound_pr" | "merge_pr" | "nudge_pipeline",
            "repo": "lkmotto/<repo>",
            "rationale": "why this step now, references concrete signal",
            "depends_on": []
          },
          ...
        ]
      }
    ]
  }

Hard rules:
1. Output {} or {"epics": []} if you have nothing high-leverage to propose.
   Empty is better than mediocre.
2. Each epic must have 3-8 steps. Single-step epics are not epics.
3. `kpi_ref` MUST exactly match a KPI title from the KPIs block. If you
   can't tie an idea to a KPI, drop it.
4. depends_on contains step.order values; step 1 has no deps.
5. Be ambitious with scope and concrete with steps. Each step should
   describe one observable change in one repo.
"""

LENS_PROMPTS["epic_bundler"] = """You are the epic bundler for the motto stack.

This lens runs AFTER the other lenses. You receive their merged proposals
in the user message under `=== UPSTREAM PROPOSALS ===`. Your job is to spot
groups of related small moves and bundle them into one ambitious move that
does the same work in fewer sessions.

Look for:
  - 2+ moves on the same repo that touch the same area
  - 2+ moves whose Claude Code prompts could share context
  - Sequences of dependent moves ("rebase PR #X then merge" → one session)
  - Janitorial moves on the same repo that could ship as one cleanup PR

Propose:
  - compound_pr moves bundling 2+ small file_issue tasks into one
  - spawn_session moves with prompts of the form: "In a single session,
    do A, then B, then C" — referencing concrete PR/issue numbers
  - merge_pr moves only when the upstream lens already cleared CI

Return an EMPTY moves list when nothing meaningful can be bundled. The
goal is fewer-bigger, not more-moves.

When you bundle, the bundled session should explicitly reference the
upstream move titles in its `intent` field so the human approver knows
which smaller moves the bundle replaces.

""" + _BASE_RULES


DEFAULT_LENSES: tuple[str, ...] = (
    "ci_doctor",
    "stale_pr_closer",
    "issue_triager",
    "cross_repo",
    "cost_watchdog",
    "architect",
    "opportunity_scout",
)
# epic_bundler runs after the others (sequential pass) so it sees their
# moves; it's NOT in DEFAULT_LENSES (which is the parallel-fanout list).
# planner is a separate sequential pass that runs heavy-only; see
# `_run_planner` below.


def _snapshot_to_prompt(snapshot: Snapshot) -> str:
    return json.dumps(asdict(snapshot), indent=2, default=str)


def _user_message(
    snapshot: Snapshot,
    *,
    strategic_intent: str = "",
    kpis: str = "",
    repo_evidence: str = "",
    upstream_proposals: str = "",
    open_kpi_epics: str = "",
) -> str:
    parts: list[str] = []
    if strategic_intent:
        parts.append(strategic_intent.rstrip() + "\n")
    if kpis:
        parts.append(kpis.rstrip() + "\n")
    if open_kpi_epics:
        parts.append(
            "===== OPEN KPI EPICS (skip proposing these) =====\n"
            + open_kpi_epics.rstrip()
            + "\n===== END OPEN KPI EPICS =====\n"
        )
    if repo_evidence:
        parts.append(repo_evidence.rstrip() + "\n")
    if upstream_proposals:
        parts.append(
            "===== UPSTREAM PROPOSALS (from sibling lenses) =====\n"
            + upstream_proposals.rstrip()
            + "\n===== END UPSTREAM PROPOSALS =====\n"
        )
    parts.append(
        "Snapshot of the motto stack:\n\n```json\n"
        + _snapshot_to_prompt(snapshot)
        + "\n```\n\nPropose your ranked next moves as strict JSON."
    )
    return "\n".join(parts)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        # strip ```json ... ```
        text = text.split("```", 2)
        text = text[1] if len(text) >= 2 else ""
        text = text[0] if isinstance(text, list) else text
    text = text.strip()
    if text.startswith("json"):
        text = text[4:].strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find a JSON object in the text
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return {}


async def _call_subagent(
    client: httpx.AsyncClient,
    *,
    lens: str,
    system: str,
    user_msg: str,
    api_key: str,
    model: str,
) -> SubagentResult:
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
                "max_tokens": 4096,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
            },
            timeout=DEEPSEEK_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        latency = int(
            (datetime.now(UTC) - started).total_seconds() * 1000
        )
        _log(
            "orchestrator.subagent.error",
            lens=lens,
            error=str(exc)[:200],
            latency_ms=latency,
        )
        return SubagentResult(
            lens=lens, moves=[], tokens_in=0, tokens_out=0,
            latency_ms=latency, error=str(exc)[:200],
        )

    latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
    text = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    usage = data.get("usage", {}) or {}
    parsed = _extract_json(text)
    raw_moves = parsed.get("moves", []) if isinstance(parsed, dict) else []

    moves: list[NextMove] = []
    if isinstance(raw_moves, list):
        for raw in raw_moves:
            if not isinstance(raw, dict):
                continue
            move = _coerce_move(raw)
            if move is not None:
                moves.append(move)

    _log(
        "orchestrator.subagent.done",
        lens=lens,
        moves=len(moves),
        tokens_in=usage.get("prompt_tokens", 0),
        tokens_out=usage.get("completion_tokens", 0),
        latency_ms=latency,
    )
    return SubagentResult(
        lens=lens,
        moves=moves,
        tokens_in=int(usage.get("prompt_tokens", 0) or 0),
        tokens_out=int(usage.get("completion_tokens", 0) or 0),
        latency_ms=latency,
    )


def _merge_moves(
    results: list[SubagentResult], *, max_total: int = 20
) -> list[NextMove]:
    """Merge subagent outputs.

    Dedupe by (repo, kind, title.lower()). When duplicates exist, keep the
    one with the lowest priority number (= highest urgency) and bump it one
    notch higher (min 1) since multiple lenses agreed.

    Sort by priority asc, then by an arbitrary stable key (repo+title) so
    output is reproducible across ticks.
    """
    by_key: dict[tuple[str, str, str], tuple[NextMove, int]] = {}
    for r in results:
        for m in r.moves:
            key = (m.repo, m.kind, m.title.lower().strip())
            if key in by_key:
                existing, agree_count = by_key[key]
                # agreement bump: lower priority number = higher urgency
                new_priority = max(
                    1, min(existing.priority, m.priority) - 1
                )
                merged = NextMove(
                    repo=existing.repo,
                    kind=existing.kind,
                    title=existing.title,
                    rationale=(
                        existing.rationale + " | " + m.rationale
                    )[:1000],
                    prompt_for_claude_code=(
                        existing.prompt_for_claude_code
                        or m.prompt_for_claude_code
                    ),
                    priority=new_priority,
                    intent=(existing.intent + " | " + m.intent)[:500],
                    code_changes=existing.code_changes or m.code_changes,
                )
                by_key[key] = (merged, agree_count + 1)
            else:
                by_key[key] = (m, 1)

    merged_list = [m for m, _ in by_key.values()]
    merged_list.sort(key=lambda m: (m.priority, m.repo, m.title))
    return merged_list[:max_total]


def _parse_planner_epics(text: str, *, run_id: str) -> list[Epic]:
    """Coerce the planner's JSON output into a list of validated Epic
    objects. Drops any epic that fails the basic shape contract.
    """
    parsed = _extract_json(text)
    if not isinstance(parsed, dict):
        return []
    raw_list = parsed.get("epics") or []
    if not isinstance(raw_list, list):
        return []
    out: list[Epic] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title", "")).strip()
        kpi_ref = str(raw.get("kpi_ref", "")).strip()
        if not title or not kpi_ref:
            continue
        steps_raw = raw.get("steps") or []
        if not isinstance(steps_raw, list) or not (3 <= len(steps_raw) <= 8):
            continue
        steps: list[EpicStep] = []
        for s in steps_raw:
            if not isinstance(s, dict):
                continue
            try:
                order = int(s.get("order", 0))
            except (TypeError, ValueError):
                order = 0
            if order < 1:
                continue
            depends_raw = s.get("depends_on") or []
            depends: list[int] = []
            if isinstance(depends_raw, list):
                for d in depends_raw:
                    try:
                        depends.append(int(d))
                    except (TypeError, ValueError):
                        continue
            steps.append(
                EpicStep(
                    order=order,
                    title=str(s.get("title", "")).strip(),
                    kind=str(s.get("kind", "spawn_session")).strip()
                    or "spawn_session",
                    repo=str(s.get("repo", "")).strip(),
                    rationale=str(s.get("rationale", "")).strip(),
                    depends_on=depends,
                )
            )
        if not (3 <= len(steps) <= 8):
            continue
        try:
            est = int(raw.get("estimated_cycles", 0))
        except (TypeError, ValueError):
            est = 0
        out.append(
            Epic(
                title=title,
                kpi_ref=kpi_ref,
                rationale=str(raw.get("rationale", "")).strip(),
                estimated_cycles=est,
                success_criteria=str(raw.get("success_criteria", "")).strip(),
                steps=steps,
                run_id=run_id,
            )
        )
    return out


async def _run_planner(
    client: httpx.AsyncClient,
    *,
    snapshot: Snapshot,
    strategic_intent: str,
    kpis_block: str,
    repo_evidence: str,
    api_key: str,
    model: str,
    run_id: str,
) -> dict[str, int]:
    """Run the planner lens (heavy-only). Inserts proposed epics into the
    epics table; returns counts dict for logging. Caller decides whether to
    invoke this based on deep_read level.
    """
    if "planner" not in LENS_PROMPTS:
        return {"skipped": 1}

    open_kpis = sorted(count_open_kpis())
    open_kpis_text = (
        "\n".join(f"- {k}" for k in open_kpis) if open_kpis
        else "(none yet — propose freely)"
    )
    user_msg = _user_message(
        snapshot,
        strategic_intent=strategic_intent,
        kpis=kpis_block,
        repo_evidence=repo_evidence,
        open_kpi_epics=open_kpis_text,
    )
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
                "max_tokens": 4096,
                "messages": [
                    {"role": "system", "content": LENS_PROMPTS["planner"]},
                    {"role": "user", "content": user_msg},
                ],
            },
            timeout=DEEPSEEK_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        _log("orchestrator.planner.error", error=str(exc)[:200])
        return {"errors": 1}

    latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
    text = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    usage = data.get("usage", {}) or {}
    candidates = _parse_planner_epics(text, run_id=run_id)
    # Skip epics whose KPI already has an open epic.
    open_set = set(open_kpis)
    fresh = [e for e in candidates if e.kpi_ref not in open_set]
    counts = insert_epics(fresh, run_id=run_id)
    counts.update(
        {
            "parsed": len(candidates),
            "filtered_kpi_dup": len(candidates) - len(fresh),
            "latency_ms": latency,
            "tokens_in": int(usage.get("prompt_tokens", 0) or 0),
            "tokens_out": int(usage.get("completion_tokens", 0) or 0),
        }
    )
    _log("orchestrator.planner.done", **counts)
    return counts


async def parallel_ideate(
    snapshot: Snapshot,
    *,
    lenses: tuple[str, ...] = DEFAULT_LENSES,
    max_concurrency: int = 5,
    run_id: str = "",
) -> list[NextMove]:
    """Fan out to N lens subagents, merge their proposals.

    Returns an empty list on configuration error (no API key) or if every
    subagent failed. Caller should fall back to the legacy `ideate()` in
    that case.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        _log("orchestrator.skipped", reason="DEEPSEEK_API_KEY missing")
        return []

    model = DEEPSEEK_DEFAULT_MODEL

    # Load strategic intent + KPIs + deep-read evidence once, share across lenses.
    strategic_intent = format_for_prompt(load_strategic_intent())
    kpis_block = format_kpis(load_kpis())
    deep_read_level = ""
    try:
        from director import deep_read
        deep_read_level = (
            os.environ.get("DIRECTOR_DEEP_READ_LEVEL", "").strip().lower()
        )
        repo_evidence = deep_read.gather_evidence(snapshot) if deep_read.is_enabled() else ""
    except Exception as exc:  # noqa: BLE001
        _log("deep_read.failed", error=str(exc)[:300])
        repo_evidence = ""

    user_msg = _user_message(
        snapshot,
        strategic_intent=strategic_intent,
        kpis=kpis_block,
        repo_evidence=repo_evidence,
    )
    sem = asyncio.Semaphore(max_concurrency)

    async with httpx.AsyncClient() as client:
        async def _run(lens: str) -> SubagentResult:
            async with sem:
                system = LENS_PROMPTS.get(lens, "")
                if not system:
                    return SubagentResult(
                        lens=lens, moves=[], tokens_in=0, tokens_out=0,
                        latency_ms=0, error="unknown lens",
                    )
                return await _call_subagent(
                    client,
                    lens=lens,
                    system=system,
                    user_msg=user_msg,
                    api_key=api_key,
                    model=model,
                )

        results = await asyncio.gather(*(_run(lens) for lens in lenses))

        # Epic bundler post-pass: see if upstream moves can be bundled.
        merged_upstream = _merge_moves(results)
        bundler_moves: list[NextMove] = []
        if merged_upstream and "epic_bundler" in LENS_PROMPTS:
            upstream_text = json.dumps(
                [asdict(m) for m in merged_upstream], indent=2, default=str
            )
            bundler_user_msg = _user_message(
                snapshot,
                strategic_intent=strategic_intent,
                repo_evidence=repo_evidence,
                upstream_proposals=upstream_text,
            )
            bundler_result = await _call_subagent(
                client,
                lens="epic_bundler",
                system=LENS_PROMPTS["epic_bundler"],
                user_msg=bundler_user_msg,
                api_key=api_key,
                model=model,
            )
            results.append(bundler_result)
            bundler_moves = bundler_result.moves
            _log(
                "orchestrator.bundler.done",
                bundled_moves=len(bundler_moves),
                tokens_in=bundler_result.tokens_in,
                tokens_out=bundler_result.tokens_out,
                latency_ms=bundler_result.latency_ms,
            )

    total_in = sum(r.tokens_in for r in results)
    total_out = sum(r.tokens_out for r in results)
    failures = [r for r in results if r.error]
    _log(
        "orchestrator.fanout.done",
        lenses=len(results),
        failed=len(failures),
        total_tokens_in=total_in,
        total_tokens_out=total_out,
        moves_per_lens={r.lens: len(r.moves) for r in results},
        deep_read_bytes=len(repo_evidence.encode("utf-8")) if repo_evidence else 0,
        strategic_intent_bytes=len(strategic_intent.encode("utf-8")) if strategic_intent else 0,
    )

    merged = _merge_moves(results)

    # Planner pass (heavy-only): propose new multi-cycle epics into the
    # epics table. Doesn't return moves itself; epic_executor handles that
    # on this and future cycles.
    if deep_read_level == "heavy":
        try:
            async with httpx.AsyncClient() as planner_client:
                await _run_planner(
                    planner_client,
                    snapshot=snapshot,
                    strategic_intent=strategic_intent,
                    kpis_block=kpis_block,
                    repo_evidence=repo_evidence,
                    api_key=api_key,
                    model=model,
                    run_id=run_id,
                )
        except Exception as exc:  # noqa: BLE001
            _log("orchestrator.planner.exception", error=str(exc)[:300])

    # Epic executor (every cycle): pull next step from each active epic
    # and append to the merged move list. Goes through the same approval
    # gate; dedupe index protects against double-queueing.
    try:
        epic_moves = epic_executor_mod.queue_next_steps()
    except Exception as exc:  # noqa: BLE001
        _log("orchestrator.epic_executor.exception", error=str(exc)[:300])
        epic_moves = []
    if epic_moves:
        # Use the same merge rules so duplicates with lens output collapse.
        synth_result = SubagentResult(
            lens="epic_executor",
            moves=epic_moves,
            tokens_in=0, tokens_out=0, latency_ms=0,
        )
        merged = _merge_moves(results + [synth_result])
        _log(
            "orchestrator.epic_executor.appended",
            epic_moves=len(epic_moves),
            merged_total=len(merged),
        )

    return merged


def is_enabled() -> bool:
    """True when DIRECTOR_PARALLEL_SUBAGENTS env is set to a truthy value."""
    return os.environ.get(
        "DIRECTOR_PARALLEL_SUBAGENTS", ""
    ).strip().lower() in ("1", "true", "yes", "on")
