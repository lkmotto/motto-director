"""Ideate: ask an LLM (Anthropic by default; falls back to Groq, OpenRouter)
to rank next moves for the motto stack."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Literal

import httpx
from anthropic import Anthropic

from director.perceive import Snapshot

MoveKind = Literal[
    "spawn_session", "file_issue", "merge_pr", "nudge_pipeline", "noop"
]

ANTHROPIC_MODEL = os.environ.get("DIRECTOR_MODEL", "claude-opus-4-7")
GROQ_MODEL = os.environ.get("DIRECTOR_GROQ_MODEL", "llama-3.3-70b-versatile")
OPENROUTER_MODEL = os.environ.get(
    "DIRECTOR_OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet"
)

_PROVIDER_ORDER: tuple[str, ...] = ("anthropic", "groq", "openrouter")

SYSTEM_PROMPT = """You are the labor-utilization director for the motto stack.

The motto stack consists of four repos:
- motto-social-agent: outbound social automation
- motto-sdr-agent: sales-development reps
- motto-appraisal-pipeline: backend pipeline that produces appraisals
- motto-appraisal-cockpit: human-in-the-loop UI

Your job: given a Snapshot of repo + queue state, propose a ranked list of
next moves that maximize productive throughput across the stack. Prefer moves
that unblock humans, ship merged work, or reactivate stalled automation.

Hard rules:
1. Every move MUST include explicit `intent` (1-2 sentences explaining WHY now,
   referencing concrete signals from the snapshot — PR numbers, ages, statuses).
   Moves without intent are dropped.
2. `kind` is one of: spawn_session, file_issue, merge_pr, nudge_pipeline, noop.
3. `priority` is an integer 1-5 (1 = highest).
4. `prompt_for_claude_code` is required for spawn_session moves; it must be a
   self-contained brief a fresh Claude Code session can act on.
5. Output STRICT JSON: {"moves": [NextMove, ...]} with no prose.
"""


@dataclass
class NextMove:
    repo: str
    kind: MoveKind
    title: str
    rationale: str
    prompt_for_claude_code: str
    priority: int
    intent: str


def _snapshot_to_prompt(snapshot: Snapshot) -> str:
    return json.dumps(asdict(snapshot), indent=2, default=str)


def _coerce_move(raw: dict) -> NextMove | None:
    intent = (raw.get("intent") or "").strip()
    if not intent:
        return None  # hard requirement: drop moves without intent
    kind = raw.get("kind")
    if kind not in {"spawn_session", "file_issue", "merge_pr", "nudge_pipeline", "noop"}:
        return None
    try:
        priority = int(raw.get("priority", 5))
    except (TypeError, ValueError):
        priority = 5
    priority = max(1, min(5, priority))
    return NextMove(
        repo=str(raw.get("repo", "")),
        kind=kind,  # type: ignore[arg-type]
        title=str(raw.get("title", "")).strip(),
        rationale=str(raw.get("rationale", "")).strip(),
        prompt_for_claude_code=str(raw.get("prompt_for_claude_code", "")).strip(),
        priority=priority,
        intent=intent,
    )


def _provider_chain() -> list[str]:
    primary = os.environ.get("LLM_PROVIDER", "anthropic").lower()
    rest = [p for p in _PROVIDER_ORDER if p != primary]
    chain = [primary, *rest] if primary in _PROVIDER_ORDER else list(_PROVIDER_ORDER)
    return chain


def _call_anthropic(client: Anthropic, system: str, user_msg: str) -> str:
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )


def _call_openai_compatible(
    *, base_url: str, api_key: str, model: str, system: str, user_msg: str
) -> str:
    r = httpx.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        },
        timeout=60.0,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _try_provider(provider: str, system: str, user_msg: str) -> str | None:
    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        return _call_anthropic(Anthropic(), system, user_msg)
    if provider == "groq":
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            return None
        return _call_openai_compatible(
            base_url="https://api.groq.com/openai/v1",
            api_key=key,
            model=GROQ_MODEL,
            system=system,
            user_msg=user_msg,
        )
    if provider == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            return None
        return _call_openai_compatible(
            base_url="https://openrouter.ai/api/v1",
            api_key=key,
            model=OPENROUTER_MODEL,
            system=system,
            user_msg=user_msg,
        )
    return None


def ideate(snapshot: Snapshot, *, client: Anthropic | None = None) -> list[NextMove]:
    """Call an LLM and return a ranked list of NextMove objects.

    If `client` is provided, use it directly (test override). Otherwise walk the
    provider chain (LLM_PROVIDER first; default 'anthropic'), trying groq and
    openrouter as cost-aware fallbacks if the primary is unavailable or errors.
    """
    user_msg = (
        "Snapshot of the motto stack:\n\n```json\n"
        + _snapshot_to_prompt(snapshot)
        + "\n```\n\nPropose the ranked next moves as strict JSON."
    )

    text: str | None = None
    if client is not None:
        text = _call_anthropic(client, SYSTEM_PROMPT, user_msg)
    else:
        for provider in _provider_chain():
            try:
                text = _try_provider(provider, SYSTEM_PROMPT, user_msg)
                if text:
                    break
            except Exception:
                continue

    if not text:
        return []

    payload = _extract_json(text)
    raw_moves = payload.get("moves", []) if isinstance(payload, dict) else []

    moves: list[NextMove] = []
    for raw in raw_moves:
        if not isinstance(raw, dict):
            continue
        move = _coerce_move(raw)
        if move is not None:
            moves.append(move)

    moves.sort(key=lambda m: m.priority)
    return moves


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        # strip code fences
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # last-resort: try to locate a JSON object
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return {}
