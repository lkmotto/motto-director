"""Phase 5.8: weekly director-meta self-improvement cron.

Reads recent fleet decisions + downstream PR outcomes, asks an LLM for 1-3
small high-confidence edits to director/policy.py or skills/*.md, and files
ONE advisory draft PR per run. Meta-PRs carry the `director-meta` label and
NEVER `auto-merge-ok` — they always require human review.

No-ops gracefully when fleet/MCP/LLM/GitHub env vars are unset.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from director import fleet
from director.ideate import (
    _provider_chain,
    _ProviderHTTPError,
    _ProviderUnavailable,
    _try_provider,
)
from director.observability import event, init_observability, register, track_run

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
SELF_REPO = "lkmotto/motto-director"
META_LABEL = "director-meta"
META_BRANCH_PREFIX = "director/meta/"

CONFIDENCE_THRESHOLD = 0.7
MAX_IMPROVEMENTS_PER_RUN = 3

META_SYSTEM_PROMPT = """You are the meta-director for motto-director.

You read a JSON dump of the director's recent outcomes — sessions spawned,
PRs merged, PRs closed unmerged, PRs reverted, common failure patterns —
and propose 1-3 small concrete edits to the director's own brain that
would have prevented the failures or accelerated the successes.

Hard rules:
1. Each improvement is small (1-3 lines of effective change). No rewrites.
2. `target_file` must be `director/policy.py` or `skills/<name>.md`.
3. `confidence` is your honest 0-1 calibration; below 0.7 is dropped.
4. `proposed_change` is a unified diff or before/after replace block.
5. Output STRICT JSON: {"improvements": [...]} with no prose.

If nothing is actionable, return {"improvements": []}.
"""


@dataclass
class Improvement:
    target_file: str
    current_excerpt: str
    proposed_change: str
    rationale: str
    confidence: float


def _log(event_name: str, **fields: object) -> None:
    record = {"ts": datetime.now(UTC).isoformat(), "event": event_name, **fields}
    print(json.dumps(record, default=str), flush=True)


def _gh_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# ── 1. gather_outcomes ─────────────────────────────────────────────────────────


async def gather_outcomes(since_days: int = 7) -> dict[str, Any]:
    """Read recent decisions + their downstream PR outcomes.

    Returns the dict shape the spec calls for. Empty fields when fleet/MCP
    isn't reachable — caller still proceeds, files no PR, exits 0.
    """
    since_minutes = since_days * 24 * 60
    events = await fleet.recent_events(
        since_minutes=since_minutes, agent_name="motto-director", limit=2000
    )
    decisions = [e for e in events if e.get("kind") == "decision"]
    spawn_events = [
        d for d in decisions if (d.get("payload") or {}).get("choice") == "spawned_claude_session"
    ]
    merge_events = [d for d in decisions if (d.get("payload") or {}).get("choice") == "merged_pr"]

    outcomes: dict[str, Any] = {
        "sessions_spawned": len(spawn_events),
        "sessions_succeeded": 0,
        "sessions_failed": 0,
        "prs_merged": [],
        "prs_closed_unmerged": [],
        "prs_reverted": [],
        "common_failure_patterns": [],
    }

    # Probe the GH PR list per repo touched by spawn decisions — we don't know
    # the PR number at spawn time (the session creates it later), so match by
    # repo+title.
    repos_to_probe = {
        ((s.get("payload") or {}).get("evidence") or {}).get("repo") for s in spawn_events
    }
    repos_to_probe.discard(None)
    repos_to_probe.discard("")

    pr_index: dict[str, list[dict[str, Any]]] = {}
    if repos_to_probe and os.environ.get("GITHUB_TOKEN"):
        async with httpx.AsyncClient(timeout=30.0) as client:
            for repo in repos_to_probe:
                pr_index[repo] = await _fetch_recent_prs(client, repo)

    for spawn in spawn_events:
        evidence = (spawn.get("payload") or {}).get("evidence") or {}
        repo = evidence.get("repo") or ""
        title = (evidence.get("title") or "").strip()
        match = _match_pr_by_title(pr_index.get(repo, []), title)
        if match is None:
            outcomes["sessions_failed"] += 1
            continue
        bucket = _classify_pr(match)
        record = {
            "repo": repo,
            "num": match.get("number"),
            "title": match.get("title"),
            "files_changed": match.get("changed_files") or 0,
            "lines_changed": (match.get("additions") or 0) + (match.get("deletions") or 0),
            "kind": "spawn_session",
        }
        if bucket == "merged":
            outcomes["sessions_succeeded"] += 1
            outcomes["prs_merged"].append(record)
        elif bucket == "closed":
            outcomes["sessions_failed"] += 1
            outcomes["prs_closed_unmerged"].append(record)
        elif bucket == "reverted":
            outcomes["sessions_failed"] += 1
            outcomes["prs_reverted"].append(record)
        # bucket == "open" — neither success nor failure yet; don't count.

    for m in merge_events:
        evidence = (m.get("payload") or {}).get("evidence") or {}
        outcomes["prs_merged"].append(
            {
                "repo": evidence.get("repo"),
                "num": evidence.get("pr_number"),
                "title": "",
                "files_changed": 0,
                "lines_changed": 0,
                "kind": "merge_pr",
            }
        )

    outcomes["common_failure_patterns"] = _summarize_failure_patterns(events)
    return outcomes


async def _fetch_recent_prs(client: httpx.AsyncClient, repo: str) -> list[dict[str, Any]]:
    try:
        r = await client.get(
            f"{GITHUB_API}/repos/{repo}/pulls",
            params={"state": "all", "per_page": 50, "sort": "updated", "direction": "desc"},
            headers=_gh_headers(),
        )
        if r.status_code != 200:
            return []
        return r.json()
    except httpx.HTTPError as e:
        logger.warning("fetch_recent_prs %s failed: %s", repo, e)
        return []


def _match_pr_by_title(prs: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    """Best-effort title match — spawn titles often become PR titles verbatim
    or with a `feat:`/`fix:` prefix, so check substring + reverse."""
    if not title:
        return None
    lowered = title.lower()
    for pr in prs:
        pr_title = (pr.get("title") or "").lower()
        if lowered == pr_title or lowered in pr_title or pr_title in lowered:
            return pr
    return None


def _classify_pr(pr: dict[str, Any]) -> str:
    if pr.get("merged_at"):
        return "merged"
    if pr.get("state") == "closed":
        return "closed"
    return "open"


def _summarize_failure_patterns(events: list[dict[str, Any]]) -> list[str]:
    """Cheap deterministic substring scan; full analysis is the LLM's job."""
    patterns: dict[str, int] = {}
    needles = (
        "ruff format",
        "ruff check",
        "pytest",
        "ImportError",
        "ModuleNotFoundError",
        "merge conflict",
        "policy:",
        "self-modifying path",
        "no_oauth_token",
    )
    for e in events:
        if e.get("level") not in ("warn", "error", "info"):
            continue
        blob = json.dumps(e.get("payload") or {}, default=str).lower()
        for n in needles:
            if n.lower() in blob:
                patterns[n] = patterns.get(n, 0) + 1
    return [k for k, v in sorted(patterns.items(), key=lambda kv: -kv[1]) if v >= 2]


# ── 2. synthesize_improvements ────────────────────────────────────────────────


async def synthesize_improvements(outcomes: dict[str, Any]) -> list[Improvement]:
    """Ask the LLM for concrete policy/skill edits. Filter low-confidence."""
    if not _any_llm_provider_configured():
        _log("meta.no_llm_provider")
        return []

    user_msg = (
        "Director outcomes from the last week:\n\n```json\n"
        + json.dumps(outcomes, indent=2, default=str)
        + "\n```\n\nPropose at most 3 small improvements as strict JSON."
    )
    text = _call_llm_chain(META_SYSTEM_PROMPT, user_msg)
    if not text:
        return []

    payload = _extract_json(text)
    raw = payload.get("improvements", []) if isinstance(payload, dict) else []
    out: list[Improvement] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        target = str(item.get("target_file", "")).strip()
        if not _target_file_allowed(target) or confidence < CONFIDENCE_THRESHOLD:
            continue
        out.append(
            Improvement(
                target_file=target,
                current_excerpt=str(item.get("current_excerpt", "")),
                proposed_change=str(item.get("proposed_change", "")),
                rationale=str(item.get("rationale", "")).strip(),
                confidence=confidence,
            )
        )
    out.sort(key=lambda i: i.confidence, reverse=True)
    return out[:MAX_IMPROVEMENTS_PER_RUN]


def _any_llm_provider_configured() -> bool:
    keys = (
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
    )
    return any(os.environ.get(k) for k in keys)


def _target_file_allowed(target: str) -> bool:
    """Restrict meta-edits to policy.py and skill files. The system prompt
    declares this contract; we also enforce it server-side."""
    if target == "director/policy.py":
        return True
    return target.startswith("skills/") and target.endswith(".md")


def _call_llm_chain(system: str, user_msg: str) -> str:
    """Same provider chain as ideate.ideate(). Reuses ideate._try_provider so
    failover semantics stay identical without refactoring ideate."""
    chain = _provider_chain()
    for i, provider in enumerate(chain):
        nxt = chain[i + 1] if i + 1 < len(chain) else None
        try:
            result = _try_provider(provider, system, user_msg)
        except _ProviderUnavailable as exc:
            _log("meta.provider_failover", **{"from": provider, "to": nxt, "reason": str(exc)})
            continue
        except _ProviderHTTPError as exc:
            _log(
                "meta.provider_failover",
                **{
                    "from": provider,
                    "to": nxt,
                    "status_code": exc.status_code,
                    "reason": exc.reason,
                },
            )
            continue
        _log(
            "meta.provider_used",
            provider=provider,
            model=result.model,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
        )
        return result.text
    return ""


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return {}


# ── 3. file_meta_pr ───────────────────────────────────────────────────────────


async def file_meta_pr(improvements: list[Improvement]) -> str | None:
    """File ONE advisory PR carrying all surviving improvements as a body
    summary. Luke applies the proposed_change blocks by hand after review.

    The cron does NOT push the LLM's diff suggestions directly: confidence < 1
    means the patch is advisory, and applying without review would amount to
    weekly self-modification, which violates the self-mod guard philosophy.
    """
    if not improvements or not os.environ.get("GITHUB_TOKEN"):
        if improvements:
            _log("meta.skip_filing", reason="no GITHUB_TOKEN")
        return None

    suffix = uuid.uuid4().hex[:8]
    branch = f"{META_BRANCH_PREFIX}{datetime.now(UTC).strftime('%Y%m%d')}-{suffix}"
    title = f"Director-meta: {_summary_line(improvements)}"
    body = _render_pr_body(improvements)

    async with httpx.AsyncClient(timeout=30.0) as client:
        repo_resp = await client.get(f"{GITHUB_API}/repos/{SELF_REPO}", headers=_gh_headers())
        if repo_resp.status_code >= 300:
            _log("meta.repo_lookup_failed", status_code=repo_resp.status_code)
            return None
        default_branch = repo_resp.json().get("default_branch", "main")

        head_resp = await client.get(
            f"{GITHUB_API}/repos/{SELF_REPO}/git/ref/heads/{default_branch}",
            headers=_gh_headers(),
        )
        if head_resp.status_code >= 300:
            return None
        base_sha = head_resp.json()["object"]["sha"]

        ref_resp = await client.post(
            f"{GITHUB_API}/repos/{SELF_REPO}/git/refs",
            headers=_gh_headers(),
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        if ref_resp.status_code >= 300:
            _log("meta.create_ref_failed", status_code=ref_resp.status_code)
            return None

        # GitHub refuses PRs with no diff; commit a marker file carrying the
        # body so the diff is meaningful even before Luke applies the
        # proposed_change blocks.
        marker_path = f"docs/director-meta/{datetime.now(UTC).strftime('%Y%m%d')}-{suffix}.md"
        await client.put(
            f"{GITHUB_API}/repos/{SELF_REPO}/contents/{marker_path}",
            headers=_gh_headers(),
            json={
                "message": "director-meta: weekly self-improvement proposal",
                "branch": branch,
                "content": base64.b64encode(body.encode()).decode(),
            },
        )

        pr_resp = await client.post(
            f"{GITHUB_API}/repos/{SELF_REPO}/pulls",
            headers=_gh_headers(),
            json={
                "title": title,
                "head": branch,
                "base": default_branch,
                "body": body,
                "draft": True,
            },
        )
        if pr_resp.status_code >= 300:
            _log("meta.pr_create_failed", status_code=pr_resp.status_code)
            return None
        pr = pr_resp.json()

        # ONLY director-meta label. NEVER auto-merge-ok — hard constraint.
        await _ensure_label(client, META_LABEL)
        await client.post(
            f"{GITHUB_API}/repos/{SELF_REPO}/issues/{pr['number']}/labels",
            headers=_gh_headers(),
            json={"labels": [META_LABEL]},
        )
        return pr.get("html_url")


def _summary_line(improvements: list[Improvement]) -> str:
    if len(improvements) == 1:
        return improvements[0].rationale[:60] or "weekly improvement"
    return f"{len(improvements)} weekly improvements"


def _render_pr_body(improvements: list[Improvement]) -> str:
    lines = [
        "Auto-generated by `motto-director-meta`. Each block below is a small,",
        "high-confidence edit suggested after analyzing the last week of fleet",
        "outcomes. **These are advisory — apply by hand after reviewing.**",
        "",
        "**This PR is NOT auto-mergeable.** It carries the `director-meta` label",
        "only; `auto-merge-ok` is intentionally absent.",
        "",
    ]
    for i, imp in enumerate(improvements, 1):
        lines.append(f"## {i}. `{imp.target_file}`  (confidence: {imp.confidence:.2f})")
        lines.append("")
        lines.append(f"**Rationale:** {imp.rationale}")
        lines.append("")
        if imp.current_excerpt:
            lines += ["**Current:**", "```", imp.current_excerpt, "```", ""]
        lines += ["**Proposed change:**", "```", imp.proposed_change, "```", ""]
    return "\n".join(lines)


async def _ensure_label(client: httpx.AsyncClient, name: str) -> None:
    """Create the label if missing. 422 (already exists) is fine."""
    r = await client.post(
        f"{GITHUB_API}/repos/{SELF_REPO}/labels",
        headers=_gh_headers(),
        json={
            "name": name,
            "color": "9b59b6",
            "description": "Director self-improvement proposal",
        },
    )
    if r.status_code not in (200, 201, 422):
        _log("meta.label_create_warning", status_code=r.status_code)


# ── 4. entrypoint ─────────────────────────────────────────────────────────────


async def _run_async() -> int:
    _log("meta.start")
    await register()
    async with track_run("meta_cycle", intent="weekly self-improvement") as run:
        outcomes = await gather_outcomes(since_days=int(os.environ.get("META_WINDOW_DAYS", "7")))
        summary = {
            "sessions_spawned": outcomes["sessions_spawned"],
            "sessions_succeeded": outcomes["sessions_succeeded"],
            "sessions_failed": outcomes["sessions_failed"],
            "prs_merged": len(outcomes["prs_merged"]),
        }
        _log("meta.outcomes", **summary)
        await event("meta.outcomes", summary, run=run)
        improvements = await synthesize_improvements(outcomes)
        _log("meta.improvements", count=len(improvements))
        await event("meta.improvements", {"count": len(improvements)}, run=run)
        pr_url = await file_meta_pr(improvements)
        run.summary["improvements"] = len(improvements)
        run.summary["pr_url"] = pr_url or ""
        _log("meta.done", pr_url=pr_url, improvements=len(improvements))
    return 0


def run_meta() -> int:
    """Sync entry point for the `motto-director-meta` console script."""
    init_observability("motto-director")
    return asyncio.run(_run_async())


if __name__ == "__main__":
    sys.exit(run_meta())
