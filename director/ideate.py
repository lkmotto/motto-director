"""Ideate: ask an LLM to rank next moves for the motto stack.

Provider chain (May 2026): **deepseek-only**.

Historical note: this layer used to route deepseek → groq → openrouter →
anthropic → claude_max (via Claude Code CLI subprocess). After ~6 weeks of
production running, every fallback path proved structurally broken:

  - claude_max via Max subscription OAuth: 5-hour rolling cap, 120s CLI
    cold-start timeouts, and the CLI's auth-precedence rules silently
    routed through ANTHROPIC_API_KEY when both envs were set, draining
    a $0-balance API key with `result: 'Credit balance is too low'`.
    See PRs #30-#35 for the four-step subprocess fix saga that ended
    in deprecation.
  - groq: 12k TPM cap on free tier; our 33KB system+snapshot prompt
    (~13.8k tokens) blows past it on every call.
  - openrouter free tier: meta-llama/llama-3.3-70b-instruct:free is
    rate-limited upstream by Venice, returns 429 within 1-2 ticks/hour.
  - anthropic SDK direct: same $0-balance API key as claude_max.

DeepSeek V4 (released April 24, 2026) makes the multi-provider chain
unnecessary: 1M context default, $0.14/M input + $0.28/M output for
V4-Flash, OpenAI-compatible API, no rate cliff at our prompt size.
Monthly burn at our cadence is ~$2-5 for V4-Flash, ~$30 for V4-Pro.

If DeepSeek is ever down, the director will return zero moves for the
tick and retry on the next 30-min cron — that's an acceptable failure
mode for an orchestrator that runs every 30 minutes anyway.
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
    "factory_droid",
    "file_issue",
    "merge_pr",
    "nudge_pipeline",
    "compound_pr",
    "file_critique_issue",
    "propose_epic",
    "noop",
    "verify_move",
]

_VALID_KINDS: frozenset[str] = frozenset(
    (
        "spawn_session",
        "factory_droid",
        "file_issue",
        "merge_pr",
        "nudge_pipeline",
        "compound_pr",
        "file_critique_issue",
        "propose_epic",
        "noop",
        "verify_move",
    )
)

# OpenAI-compatible providers. Each entry: (key_env, default_model_env,
# default_model, base_url). DeepSeek-only as of May 2026; see module
# docstring for history. Other entries can be added back here if a
# backup provider is ever wanted.
PROVIDER_CONFIG: dict[str, dict[str, str]] = {
    "deepseek": {
        "key_env": "DEEPSEEK_API_KEY",
        "model_env": "DEEPSEEK_MODEL",
        # V4-Flash is the new floor model: 1M context, ~$0.14/M input,
        # "reasoning capabilities closely approach V4-Pro... performs on
        # par with V4-Pro on simple Agent tasks" per DeepSeek's release
        # notes. Bump to deepseek-v4-pro via DEEPSEEK_MODEL env if quality
        # ever lacks. The legacy deepseek-chat / deepseek-reasoner ids
        # are deprecated 2026-07-24.
        "default_model": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com/v1",
    },
}

# DeepSeek-only chain. No fallback. If DeepSeek is down for a 30-min
# tick, the director returns zero moves and tries again next tick.
CHAIN_ORDER: tuple[str, ...] = ("deepseek",)

# Anthropic SDK constants kept for the test-injection path in `ideate()`
# (callers can pass an Anthropic client directly for unit tests). They
# are no longer part of the production provider chain.
ANTHROPIC_MODEL_ENV = "DIRECTOR_ANTHROPIC_MODEL"
ANTHROPIC_DEFAULT_MODEL = "claude-opus-4-7"

# Claude Max constants — DEPRECATED May 2026. Kept as module-level
# attributes only because external callers (tests, other repos) may
# import them. The production code path no longer uses any of them and
# `_call_claude_max` is no longer wired into the provider chain.
CLAUDE_MAX_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_MAX_MODEL_ENV = "DIRECTOR_CLAUDE_MAX_MODEL"
CLAUDE_MAX_DEFAULT_MODEL = "claude-sonnet-4-5"
CLAUDE_MAX_BIN_ENV = "CLAUDE_CLI_BIN"
CLAUDE_MAX_DEFAULT_BIN = "claude"
CLAUDE_MAX_TIMEOUT_S = 120
CLAUDE_MAX_BETA = "oauth-2025-04-20"
CLAUDE_MAX_API_VERSION = "2023-06-01"
CLAUDE_MAX_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."

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
2. `kind` is one of: factory_droid, file_issue, merge_pr, nudge_pipeline,
   compound_pr, file_critique_issue, noop, verify_move.
   - `factory_droid` is the ONLY way to spawn a coding agent. It dispatches
     to a Factory.ai droid using one of the .factory/droids/*.md roles:
     doppler-sync (secrets), northflank-ops (deploys/crons), github-ops
     (PRs/issues/merges), ona-fleet-reporter (audits/fleet health), or
     factory-orchestrator (multi-step meta work). The director routes to
     the right droid by repo+intent keywords; you do not need to specify.
   - `spawn_session` (Claude Code) is DEPRECATED. Do NOT propose it. The
     Claude Code subscription was cancelled; the dispatcher remains only
     for backwards-compat on old pending_moves rows. Any spawn_session
     move you propose will be dropped.
   - `verify_move` triggers an outcome verification on a previously-applied
     move. Use this AFTER applying a move, when you want to confirm the
     move actually achieved its KPI intent. Set `target_move_id` in the
     code_changes/payload area to the pending_moves.id you want verified.
     Day 1 verifiers only support kind=noop and kind=merge_pr; other kinds
     return inconclusive until per-repo verifiers are wired.
3. `priority` is an integer 1-5 (1 = highest).
4. `prompt_for_claude_code` is required for factory_droid moves; it must
   be a self-contained brief a fresh droid session can act on. (Field
   name kept for backwards-compat; it's the prompt regardless of kind.)
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
    # Epic linkage (set by epic_executor when this move is the next step
    # of an active multi-cycle epic). Both fields end up in move_payload
    # so applied_step_orders() can join pending_moves to epics.
    epic_id: int | None = None
    step_order: int | None = None
    # verify_move kind: id of the pending_moves row whose outcome should
    # be verified. Persisted into move_payload by act/queue and read back
    # by _verify_move executor.
    target_move_id: int | None = None


def _log(event: str, **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


def _snapshot_to_prompt(snapshot: Snapshot) -> str:
    return json.dumps(asdict(snapshot), indent=2, default=str)


# Move kinds the planner is no longer allowed to propose. The dispatcher
# in act.py still handles them (for old pending_moves rows from before the
# deprecation), but new proposals are dropped here so DeepSeek can't keep
# spawning Claude Code sessions on a cancelled subscription.
_DEPRECATED_PROPOSAL_KINDS: frozenset[str] = frozenset({"spawn_session"})


def _coerce_move(raw: dict) -> NextMove | None:
    intent = (raw.get("intent") or "").strip()
    if not intent:
        return None  # hard requirement: drop moves without intent
    kind = raw.get("kind")
    if kind not in _VALID_KINDS:
        return None
    if kind in _DEPRECATED_PROPOSAL_KINDS:
        _log(
            "move.dropped",
            reason="deprecated_proposal_kind",
            kind=kind,
            title=raw.get("title", ""),
        )
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
                code_changes.append({"path": str(c["path"]), "content": str(c["content"])})
    epic_id_raw = raw.get("epic_id")
    step_order_raw = raw.get("step_order")
    try:
        epic_id_val = int(epic_id_raw) if epic_id_raw is not None else None
    except (TypeError, ValueError):
        epic_id_val = None
    try:
        step_order_val = int(step_order_raw) if step_order_raw is not None else None
    except (TypeError, ValueError):
        step_order_val = None
    return NextMove(
        repo=str(raw.get("repo", "")),
        kind=kind,  # type: ignore[arg-type]
        title=str(raw.get("title", "")).strip(),
        rationale=str(raw.get("rationale", "")).strip(),
        prompt_for_claude_code=str(raw.get("prompt_for_claude_code", "")).strip(),
        priority=priority,
        intent=intent,
        code_changes=code_changes,
        epic_id=epic_id_val,
        step_order=step_order_val,
    )


def _provider_chain() -> list[str]:
    # DeepSeek-only as of May 2026 (see module docstring). LLM_PROVIDER env
    # can pin a different primary if more entries are ever added back.
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


def _call_claude_max(system: str, user_msg: str) -> _ProviderResult:
    """Invoke the Claude Max subscription via the official `claude` CLI.

    Why subprocess and not /v1/messages directly:
        Anthropic blocks subscription OAuth tokens from the bare API as of
        Jan 9, 2026 (formalized Feb 19, 2026). The CLI is the authorized
        client; calling it from a subprocess is the supported path.

    Token plumbing:
        CLAUDE_CODE_OAUTH_TOKEN must be set in the process env. The CLI
        picks it up automatically; we don't pass it on the command line.

    Output handling:
        We invoke `claude -p <prompt> --output-format json --max-turns 1
        --append-system-prompt <system>`. The CLI emits a single JSON object
        with {result, total_cost_usd, num_turns, ...}. We do NOT use
        --system-prompt because that overrides Claude Code's own system
        prompt, which is exactly what kills subscription billing identity.
        --append-system-prompt preserves it.

    Failure modes mapped onto the chain's failover taxonomy:
        - missing token / missing binary  → _ProviderUnavailable (try next provider)
        - non-zero exit / timeout / parse → _ProviderHTTPError (treat as 502)
    """
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    if not os.environ.get(CLAUDE_MAX_TOKEN_ENV):
        raise _ProviderUnavailable(f"{CLAUDE_MAX_TOKEN_ENV} not set")
    bin_path = os.environ.get(CLAUDE_MAX_BIN_ENV, CLAUDE_MAX_DEFAULT_BIN)
    if shutil.which(bin_path) is None:
        raise _ProviderUnavailable(f"`{bin_path}` CLI not on PATH")

    model = os.environ.get(CLAUDE_MAX_MODEL_ENV, CLAUDE_MAX_DEFAULT_MODEL)
    cmd = [
        bin_path,
        "-p",
        user_msg,
        "--output-format",
        "json",
        "--max-turns",
        "1",
        "--model",
        model,
        "--append-system-prompt",
        system,
        # Northflank containers run as root. The Claude Code CLI refuses to
        # operate non-interactively under root unless this flag is set
        # (see anthropics/claude-code#3490, #2951, #9184). Safe here because
        # `--max-turns 1` means the model returns a single JSON response and
        # never invokes a tool — there are no permission prompts to bypass.
        "--dangerously-skip-permissions",
    ]
    # Companion to --dangerously-skip-permissions: the CLI also requires
    # IS_SANDBOX=1 in env to confirm the operator understands they are
    # running in a sandbox-like environment (root container, ephemeral fs).
    sub_env = {**os.environ, "IS_SANDBOX": "1"}
    # CRITICAL: strip API-key envs from the subprocess env. Per Anthropic
    # auth precedence (https://docs.anthropic.com/en/docs/claude-code/iam):
    #   1. cloud provider creds (BEDROCK/VERTEX/FOUNDRY)
    #   2. ANTHROPIC_AUTH_TOKEN  (LLM gateways)
    #   3. ANTHROPIC_API_KEY     (direct API; "In non-interactive mode (-p),
    #                              the key is always used when present.")
    #   4. apiKeyHelper
    #   5. CLAUDE_CODE_OAUTH_TOKEN  <- this is what we want
    #   6. interactive /login
    # If ANTHROPIC_API_KEY is in env (it is — `motto-core/prd` ships it for
    # the anthropic-sdk fallback in the provider chain), the CLI silently
    # routes calls through the bare API. If that key has a $0 balance the
    # CLI returns `result: "Credit balance is too low"` with tokens_in=0,
    # tokens_out=0, total_cost_usd=0, exit_code=1. We've been silently
    # producing zero moves for this exact reason. Strip the API-key envs
    # so the CLI falls through to CLAUDE_CODE_OAUTH_TOKEN (#5) and bills
    # the Max subscription instead.
    sub_env.pop("ANTHROPIC_API_KEY", None)
    sub_env.pop("ANTHROPIC_AUTH_TOKEN", None)
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_MAX_TIMEOUT_S,
            check=False,
            env=sub_env,
            # The Claude Code CLI inspects stdin even when `-p <text>` provides
            # the prompt: if stdin is open it will pause for ~3s waiting for
            # piped input, then warn and fall through. In a Northflank cron
            # the inherited stdin is a closed/orphan TTY that confuses the CLI
            # and causes a non-zero exit. Pin stdin to /dev/null so the CLI
            # treats `-p` as the sole input source.
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise _ProviderHTTPError(504, f"claude CLI timeout after {CLAUDE_MAX_TIMEOUT_S}s") from exc
    except FileNotFoundError as exc:  # pragma: no cover -- shutil.which guards this
        raise _ProviderUnavailable(f"claude CLI vanished mid-run: {exc}") from exc

    # The CLI sometimes emits a fully-formed JSON result on stdout AND exits
    # non-zero (observed under root containers + IS_SANDBOX=1). When that
    # happens, prefer parsing stdout over treating the exit code as fatal.
    stdout_text = (result.stdout or "").strip()
    parsed_body: dict | None = None
    if stdout_text:
        try:
            parsed_body = json.loads(stdout_text)
        except json.JSONDecodeError:
            parsed_body = None

    if result.returncode != 0:
        # If we got a parseable body with a `result` field, treat the exit
        # code as advisory (the run actually produced output). Log it for
        # observability but don't fail the provider.
        if parsed_body and isinstance(parsed_body, dict) and parsed_body.get("result"):
            _log(
                "claude_max.advisory_exit",
                exit_code=result.returncode,
                terminal_reason=parsed_body.get("terminal_reason"),
                permission_denials=len(parsed_body.get("permission_denials") or []),
            )
        else:
            err_tail = (result.stderr or "").strip()[-400:]
            out_head = stdout_text[:600]
            out_tail = stdout_text[-600:]
            raise _ProviderHTTPError(
                502,
                (
                    f"claude CLI exit={result.returncode} "
                    f"stderr={err_tail!r} "
                    f"stdout_head={out_head!r} "
                    f"stdout_tail={out_tail!r}"
                ),
            )

    if parsed_body is None:
        raise _ProviderHTTPError(502, f"claude CLI bad JSON: {stdout_text[:600]}")

    text = parsed_body.get("result") or ""
    usage = parsed_body.get("usage") or {}
    # Diagnostic: the CLI JSON shape has shifted across versions. Log the
    # top-level keys + usage keys + result text length so we can see why
    # tokens_in/tokens_out come back as 0 and confirm `result` is populated.
    _log(
        "claude_max.body_shape",
        body_keys=sorted(parsed_body.keys()),
        usage_keys=sorted(usage.keys()) if isinstance(usage, dict) else [],
        usage_value=usage if isinstance(usage, dict) else None,
        result_text_len=len(text),
        result_text_head=text[:300],
        terminal_reason=parsed_body.get("terminal_reason"),
        num_turns=parsed_body.get("num_turns"),
        total_cost_usd=parsed_body.get("total_cost_usd"),
    )
    # Some CLI versions nest token counts under message.usage instead of
    # the top-level usage key. Fall back to that shape if needed.
    if not usage and isinstance(parsed_body.get("message"), dict):
        msg_usage = parsed_body["message"].get("usage") or {}
        if isinstance(msg_usage, dict):
            usage = msg_usage
    return _ProviderResult(
        text=text,
        model=parsed_body.get("model", model),
        tokens_in=usage.get("input_tokens", 0) if isinstance(usage, dict) else 0,
        tokens_out=usage.get("output_tokens", 0) if isinstance(usage, dict) else 0,
    )


def _call_anthropic(client: Anthropic, system: str, user_msg: str) -> _ProviderResult:
    model = os.environ.get(ANTHROPIC_MODEL_ENV, ANTHROPIC_DEFAULT_MODEL)
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
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
    if provider == "claude_max":
        # DEPRECATED — retained for tests that exercise the old path. Not
        # in CHAIN_ORDER, so production never reaches this branch.
        return _call_claude_max(system, user_msg)
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
            response_text_len=len(result.text or ""),
            response_text_head=(result.text or "")[:500],
        )
        moves = _parse_text_to_moves(result.text)
        # Diagnostic: when zero moves are produced, log enough of the raw
        # response to debug whether the model returned `{"moves":[]}`,
        # malformed JSON, a refusal, or wrapped its output unexpectedly.
        if not moves:
            _log(
                "ideate.zero_moves",
                provider=provider,
                model=result.model,
                response_text_len=len(result.text or ""),
                response_text_full=(result.text or "")[:4000],
                prompt_user_msg_len=len(user_msg),
            )
        return moves

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
