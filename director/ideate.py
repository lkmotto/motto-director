"""Ideate: ask an LLM to rank next moves for the motto stack.

Default provider chain: deepseek → groq → openrouter → anthropic. Anthropic
is last (Anthropic API credit is the bottleneck this layer was built to dodge);
all three primary providers are OpenAI-compatible and routed through the
openai SDK with a base_url + api_key swap.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal

from anthropic import Anthropic
from openai import OpenAI

from director.perceive import Snapshot

MoveKind = Literal[
    "spawn_session",
    "file_issue",
    "merge_pr",
    "nudge_pipeline",
    "compound_pr",
    "noop",
]

_VALID_KINDS: frozenset[str] = frozenset(
    ("spawn_session", "file_issue", "merge_pr", "nudge_pipeline", "compound_pr", "noop")
)

# OpenAI-compatible providers. Each entry: (key_env, default_model_env,
# default_model, base_url).
PROVIDER_CONFIG: dict[str, dict[str, str]] = {
    "deepseek": {
        "key_env": "DEEPSEEK_API_KEY",
        "model_env": "DEEPSEEK_MODEL",
        "default_model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
    },
    "groq": {
        "key_env": "GROQ_API_KEY",
        "model_env": "GROQ_MODEL",
        "default_model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "openrouter": {
        "key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "default_model": "meta-llama/llama-3.3-70b-instruct:free",
        "base_url": "https://openrouter.ai/api/v1",
    },
}

CHAIN_ORDER: tuple[str, ...] = ("deepseek", "groq", "openrouter", "anthropic")
ANTHROPIC_MODEL_ENV = "DIRECTOR_ANTHROPIC_MODEL"
ANTHROPIC_DEFAULT_MODEL = "claude-opus-4-7"

_FAILOVER_STATUSES: frozenset[int] = frozenset({401, 402, 429, 500, 502, 503, 504})

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
2. `kind` is one of: spawn_session, file_issue, merge_pr, nudge_pipeline,
   compound_pr, noop.
3. `priority` is an integer 1-5 (1 = highest).
4. `prompt_for_claude_code` is required for spawn_session moves; it must be a
   self-contained brief a fresh Claude Code session can act on.
5. For `compound_pr` moves, populate `code_changes` as a list of
   {path, content} objects with the full new file content. The director
   appends these as a single commit to a long-lived rolling PR per repo.
6. Output STRICT JSON: {"moves": [NextMove, ...]} with no prose.
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
    code_changes: list[dict[str, str]] = field(default_factory=list)


def _log(event: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


def _snapshot_to_prompt(snapshot: Snapshot) -> str:
    return json.dumps(asdict(snapshot), indent=2, default=str)


def _coerce_move(raw: dict) -> NextMove | None:
    intent = (raw.get("intent") or "").strip()
    if not intent:
        return None  # hard requirement: drop moves without intent
    kind = raw.get("kind")
    if kind not in _VALID_KINDS:
        return None
    try:
        priority = int(raw.get("priority", 5))
    except (TypeError, ValueError):
        priority = 5
    priority = max(1, min(5, priority))
    code_changes: list[dict[str, str]] = []
    raw_changes = raw.get("code_changes") or []
    if isinstance(raw_changes, list):
        for c in raw_changes:
            if isinstance(c, dict) and "path" in c and "content" in c:
                code_changes.append(
                    {"path": str(c["path"]), "content": str(c["content"])}
                )
    return NextMove(
        repo=str(raw.get("repo", "")),
        kind=kind,  # type: ignore[arg-type]
        title=str(raw.get("title", "")).strip(),
        rationale=str(raw.get("rationale", "")).strip(),
        prompt_for_claude_code=str(raw.get("prompt_for_claude_code", "")).strip(),
        priority=priority,
        intent=intent,
        code_changes=code_changes,
    )


def _provider_chain() -> list[str]:
    primary = os.environ.get("LLM_PROVIDER", "deepseek").lower()
    rest = [p for p in CHAIN_ORDER if p != primary]
    return [primary, *rest] if primary in CHAIN_ORDER else list(CHAIN_ORDER)


@dataclass
class _ProviderResult:
    text: str
    model: str
    tokens_in: int
    tokens_out: int


class _ProviderUnavailable(Exception):
    """Provider can't be tried (e.g. missing API key)."""


class _ProviderHTTPError(Exception):
    """Provider returned an error we should fail over from."""

    def __init__(self, status_code: int | None, reason: str):
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


def _call_anthropic(client: Anthropic, system: str, user_msg: str) -> _ProviderResult:
    model = os.environ.get(ANTHROPIC_MODEL_ENV, ANTHROPIC_DEFAULT_MODEL)
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )
    usage = getattr(response, "usage", None)
    return _ProviderResult(
        text=text,
        model=model,
        tokens_in=getattr(usage, "input_tokens", 0) if usage else 0,
        tokens_out=getattr(usage, "output_tokens", 0) if usage else 0,
    )


def _call_openai_compatible(provider: str, system: str, user_msg: str) -> _ProviderResult:
    cfg = PROVIDER_CONFIG[provider]
    api_key = os.environ.get(cfg["key_env"])
    if not api_key:
        raise _ProviderUnavailable(f"{cfg['key_env']} not set")
    model = os.environ.get(cfg["model_env"], cfg["default_model"])
    client = OpenAI(api_key=api_key, base_url=cfg["base_url"], max_retries=0)
    response = client.chat.completions.create(
        model=model,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
    )
    text = response.choices[0].message.content or ""
    usage = response.usage
    return _ProviderResult(
        text=text,
        model=model,
        tokens_in=getattr(usage, "prompt_tokens", 0) if usage else 0,
        tokens_out=getattr(usage, "completion_tokens", 0) if usage else 0,
    )


def _try_provider(provider: str, system: str, user_msg: str) -> _ProviderResult:
    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise _ProviderUnavailable("ANTHROPIC_API_KEY not set")
        try:
            return _call_anthropic(Anthropic(), system, user_msg)
        except Exception as exc:  # noqa: BLE001
            status = _extract_status(exc)
            raise _ProviderHTTPError(status, str(exc)) from exc
    if provider in PROVIDER_CONFIG:
        try:
            return _call_openai_compatible(provider, system, user_msg)
        except _ProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            status = _extract_status(exc)
            raise _ProviderHTTPError(status, str(exc)) from exc
    raise _ProviderUnavailable(f"unknown provider {provider}")


def _extract_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        if response is not None:
            status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def ideate(snapshot: Snapshot, *, client: Anthropic | None = None) -> list[NextMove]:
    """Call an LLM and return a ranked list of NextMove objects.

    If `client` is provided, use it directly (test override; Anthropic-shaped).
    Otherwise walk the provider chain (LLM_PROVIDER first; default 'deepseek'),
    failing over on missing keys, 401/402/429/5xx, or other errors.
    """
    user_msg = (
        "Snapshot of the motto stack:\n\n```json\n"
        + _snapshot_to_prompt(snapshot)
        + "\n```\n\nPropose the ranked next moves as strict JSON."
    )

    if client is not None:
        result = _call_anthropic(client, SYSTEM_PROMPT, user_msg)
        return _parse_text_to_moves(result.text)

    chain = _provider_chain()
    for i, provider in enumerate(chain):
        next_provider = chain[i + 1] if i + 1 < len(chain) else None
        try:
            result = _try_provider(provider, SYSTEM_PROMPT, user_msg)
        except _ProviderUnavailable as exc:
            _log(
                "ideate.provider_failover",
                **{
                    "from": provider,
                    "to": next_provider,
                    "status_code": None,
                    "reason": str(exc),
                },
            )
            continue
        except _ProviderHTTPError as exc:
            _log(
                "ideate.provider_failover",
                **{
                    "from": provider,
                    "to": next_provider,
                    "status_code": exc.status_code,
                    "reason": exc.reason,
                },
            )
            # Spec says fail over on 401/402/429/5xx; for other status codes we
            # also fail over (treating them as transient/unrecognized) since
            # the alternative is a dead loop.
            if exc.status_code is not None and exc.status_code not in _FAILOVER_STATUSES:
                # 4xx other than auth/payment/rate-limit — likely a permanent
                # request error; still failover to avoid sticking on a bad
                # provider, but we noted it via the failover log already.
                pass
            continue

        _log(
            "ideate.provider_used",
            provider=provider,
            model=result.model,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
        )
        return _parse_text_to_moves(result.text)

    return []


def _parse_text_to_moves(text: str) -> list[NextMove]:
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
