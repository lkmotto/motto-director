"""Ideate: ask Anthropic Opus to rank next moves for the motto stack."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Literal

from anthropic import Anthropic

from director.perceive import Snapshot

MoveKind = Literal[
    "spawn_session", "file_issue", "merge_pr", "nudge_pipeline", "noop"
]

MODEL = os.environ.get("DIRECTOR_MODEL", "claude-opus-4-7")

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


def ideate(snapshot: Snapshot, *, client: Anthropic | None = None) -> list[NextMove]:
    """Call Claude Opus and return a ranked list of NextMove objects."""
    client = client or Anthropic()
    user_msg = (
        "Snapshot of the motto stack:\n\n```json\n"
        + _snapshot_to_prompt(snapshot)
        + "\n```\n\nPropose the ranked next moves as strict JSON."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    text = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )
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
